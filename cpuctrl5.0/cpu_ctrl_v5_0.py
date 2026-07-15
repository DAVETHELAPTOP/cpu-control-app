#!/usr/bin/env python3
"""
CPU CONTROL v5.0 - real GTK desktop app for Linux Mint (all editions, incl. LMDE)
Reads live system data (frequencies, load, power, thermal, memory, disk, GPU)
and lets you change governor / max freq / boost / P-State % / RAM limits /
swap strategy through a small root-only helper invoked via pkexec (so this
app never needs to run as root itself).

v5.0 changes:
  - BOOST tab: the standalone boost.sh CLI is now merged in - lists active
    user apps (same cleaned/filtered name list boost.sh produced), lets you
    pick one and renice it (-20 to 19) via the root helper, no more running
    a separate sudo script in a terminal.
  - Settings tab: theme, UI scaling, start-minimized, refresh rate
    (fast/balanced/low-resource), background monitoring toggle, history
    length, memory warning threshold, benchmark defaults - all persisted to
    ~/.config/cpu-control/settings.json and applied immediately.
  - Drive tab: per-drive info (model, type, interface, capacity, used/free,
    filesystem, health via smartctl if available), live read/write activity
    from /proc/diskstats, and a benchmark tool (sequential/random read/write,
    IOPS, latency, performance rating) that only ever touches a temp file
    it creates and deletes itself - never user data.
  - RAM tab additions: memory pressure (PSI), cached/committed memory, a
    low-memory warning banner, and a bounded in-memory history buffer
    (deque, capped by the history-length setting) instead of unbounded
    growth.
  - Stability: every periodic tick is now wrapped so one bad reading can't
    crash the app; errors go to a rotating log file
    (~/.config/cpu-control/app.log) instead of stderr; missing
    sensors/drives/tools degrade to "N/A" instead of throwing.
  - Refresh interval is now dynamic (driven by the Settings tab) instead of
    a hardcoded 1000ms GLib timeout.

v4.0 changes:
  - RAM Limiter (formerly a separate ramlimit2.0 script) is now a tab in
    this app: per-process RAM caps (push-to-swap or hard/OOM), zswap
    control, and disk/zswap swap-ratio presets.
  - GPU tab (BETA): basic read-only GPU info - model, driver, vendor,
    VRAM, and supported video codecs where detectable.
  - Fixed CPU power-draw readings on older Intel chips whose RAPL energy
    counter is a narrow (often 32-bit) register that wraps around - power
    reads now handle that wraparound instead of silently returning junk.
  - Broader AMD support: AMD RAPL power (amd-rapl / amd_energy powercap
    zones), amd_pstate max-perf %, k10temp/zenpower temps.
  - All UI text recolored so nothing renders in plain gray/black - every
    label, title, and value uses a distinct, readable accent color.
"""

import os
import re
import glob
import time
import uuid
import shutil
import logging
import tempfile
import subprocess
import csv
import json
from collections import deque
from logging.handlers import RotatingFileHandler
from datetime import datetime

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(APP_DIR, "cpu_ctrl_helper")

CONFIG_DIR = os.path.expanduser("~/.config/cpu-control")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
LOG_FILE = os.path.join(CONFIG_DIR, "app.log")
os.makedirs(CONFIG_DIR, exist_ok=True)

# ---------------------------------------------------------------- logging
# Rotating file log so a long-running session never grows the log file
# without bound. Anything the app would otherwise crash on gets caught and
# logged here instead (see safe_call / _tick).
logger = logging.getLogger("cpu_control")
logger.setLevel(logging.INFO)
try:
    _handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
except Exception:
    pass


def safe_call(fn, *args, default=None, **kwargs):
    """Run fn and swallow/log any exception, returning `default` instead.
    Used everywhere a sensor/drive/tool might be missing or flaky so one
    bad reading can't take the whole app down."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("safe_call(%s) failed: %s", getattr(fn, "__name__", fn), e)
        return default


# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS = {
    "theme": "dark",                 # dark | light | system
    "ui_scale": 100,                 # percent
    "start_minimized": False,
    "start_with_system": False,
    "refresh_mode": "balanced",      # fast | balanced | low_resource
    "background_monitoring": True,
    "notifications_enabled": True,
    "history_length": 120,           # samples kept in memory (ring buffer)
    "mem_warning_pct": 90,           # % used RAM that triggers a warning
    "low_resource_mode": False,
    "benchmark_size_mb": 256,
    "benchmark_duration_s": 10,
    "confirm_before_benchmark": True,
    "auto_clean_benchmark_files": True,
    "logging_debug": False,
}

REFRESH_INTERVALS_MS = {"fast": 500, "balanced": 1000, "low_resource": 3000}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    data = safe_call(lambda: json.load(open(SETTINGS_FILE)), default=None)
    if isinstance(data, dict):
        for k, v in data.items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
    return s


def save_settings(settings):
    def _write():
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    return safe_call(_write, default=False)

CSS = b"""
window { background-color: #000000; }
* { font-family: 'Share Tech Mono', 'Ubuntu Mono', monospace; color: #e8f0fe; }

.logo {
    font-family: 'Orbitron', sans-serif;
    font-weight: 900;
    font-size: 15px;
    letter-spacing: 3px;
    color: #ffffff;
}
.logo-fx { color: #00ff88; }
.vbadge { color: #ffd166; font-size: 10px; letter-spacing: 1px; font-weight: bold; }
.beta-badge {
    color: #ff2d55;
    font-size: 10px;
    letter-spacing: 1px;
    font-weight: bold;
    background-color: #1a0008;
    border: 1px solid #ff2d55;
    border-radius: 4px;
    padding: 2px 6px;
}

.card {
    background-color: #060606;
    border: 1px solid #232323;
    border-radius: 8px;
}
.ctit {
    font-family: 'Orbitron', sans-serif;
    font-weight: 700;
    font-size: 10px;
    letter-spacing: 2px;
    color: #ffd166;
}
.stat-label { font-size: 10px; color: #7fd9ff; letter-spacing: 1px; font-weight: bold; }
.stat-value { font-size: 16px; color: #ffffff; font-weight: bold; }
.stat-value-green { color: #00ff88; }
.stat-value-cyan { color: #00d4ff; }
.stat-value-orange { color: #ff8c00; }
.stat-value-red { color: #ff2d55; }
.stat-value-purple { color: #c9a6ff; }

.core-label { font-size: 10px; color: #c9a6ff; font-weight: bold; }
.hint-text { font-size: 10px; color: #7fd9ff; }
.warn-text { font-size: 11px; color: #ff8c00; font-weight: bold; }
progressbar trough { background-color: #0a0a0a; border-radius: 4px; min-height: 10px; border: 1px solid #232323; }
progressbar progress { background-color: #00ff88; border-radius: 4px; min-height: 10px; }
progressbar.warn progress { background-color: #ff8c00; }
progressbar.hot progress { background-color: #ff2d55; }

label { color: #e8f0fe; }

button {
    background-color: #0a0a0a;
    border: 1px solid #2a2a2a;
    border-radius: 6px;
    color: #d8e6ff;
    padding: 6px 12px;
    font-weight: bold;
}
button:hover { background-color: #141414; border-color: #00d4ff; color: #00d4ff; }
button.accent { border-color: #00ff88; color: #00ff88; }
button.danger { border-color: #ff2d55; color: #ff2d55; }
button.info { border-color: #00d4ff; color: #00d4ff; }

combobox button, entry {
    background-color: #000000;
    border: 1px solid #2a2a2a;
    border-radius: 4px;
    color: #ffffff;
    padding: 4px 8px;
}
combobox label { color: #ffffff; }

switch { background-color: #1a1a1a; border: 1px solid #2a2a2a; }
switch:checked { background-color: #00ff88; }

notebook header {
    background-color: #060606;
    border-bottom: 2px solid #232323;
}
notebook tab {
    padding: 8px 14px;
}
notebook tab label {
    font-family: 'Orbitron', sans-serif;
    font-size: 9px;
    letter-spacing: 1.5px;
    color: #7fd9ff;
    font-weight: bold;
}
notebook tab:checked label { color: #00ff88; }

textview, textview text {
    background-color: #000000;
    color: #00ff88;
    font-family: 'Share Tech Mono', monospace;
}

.queue-big { font-size: 34px; color: #ffffff; font-weight: bold; }
.queue-big-warn { color: #ff8c00; }
.queue-big-hot { color: #ff2d55; }

scale trough { background-color: #0a0a0a; border: 1px solid #2a2a2a; }
scale highlight { background-color: #00d4ff; }
"""

# ---------------------------------------------------------------- utilities

def read_file(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def cpu_model():
    out = read_file("/proc/cpuinfo", "") or ""
    m = re.search(r"model name\s*:\s*(.+)", out)
    return m.group(1).strip() if m else "Unknown CPU"


def cpu_vendor():
    out = (read_file("/proc/cpuinfo", "") or "").lower()
    if "genuineintel" in out:
        return "Intel"
    if "authenticamd" in out:
        return "AMD"
    return "Unknown"


def core_dirs():
    return sorted(
        glob.glob("/sys/devices/system/cpu/cpu[0-9]*"),
        key=lambda p: int(re.search(r"cpu(\d+)$", p).group(1)),
    )


def core_freqs_mhz():
    freqs = []
    for d in core_dirs():
        f = read_file(f"{d}/cpufreq/scaling_cur_freq")
        freqs.append(int(f) // 1000 if f else None)
    return freqs


_LAST_CPU_TIMES = {}


def _read_proc_stat_percpu():
    """Return {'cpu0': (idle_ticks, total_ticks), ...} from /proc/stat."""
    out = read_file("/proc/stat", "") or ""
    result = {}
    for line in out.splitlines():
        if not line.startswith("cpu") or len(line) < 4 or not line[3].isdigit():
            continue
        parts = line.split()
        name = parts[0]
        nums = list(map(int, parts[1:]))
        # user nice system idle iowait irq softirq steal ...
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        result[name] = (idle, total)
    return result


def core_loads_pct():
    """Per-core CPU usage as a 0-100 percentage, based on the delta since the last tick."""
    global _LAST_CPU_TIMES
    cur = _read_proc_stat_percpu()
    names = sorted(cur.keys(), key=lambda n: int(n[3:]))
    loads = []
    for name in names:
        idle, total = cur[name]
        prev = _LAST_CPU_TIMES.get(name)
        if prev:
            didle = idle - prev[0]
            dtotal = total - prev[1]
            pct = 100.0 * (1 - (didle / dtotal)) if dtotal > 0 else 0.0
        else:
            pct = 0.0
        loads.append(max(0, min(100, round(pct))))
    _LAST_CPU_TIMES = cur
    return loads


def waiting_for_cpu():
    """Approx. number of processes ready to run but stuck waiting for a free core.

    /proc/loadavg's 4th field is 'currently_runnable/total_processes'. Runnable
    processes beyond the number of logical cores are, by definition, waiting
    in the run queue for a core to free up.
    """
    out = read_file("/proc/loadavg", "") or ""
    parts = out.split()
    if len(parts) < 4 or "/" not in parts[3]:
        return 0
    try:
        running = int(parts[3].split("/")[0])
    except ValueError:
        return 0
    ncores = len(core_dirs()) or 1
    return max(0, running - ncores)


def current_governor():
    return read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "unknown")


def available_governors():
    g = read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors", "")
    return g.split() if g else []


def freq_bounds_mhz():
    lo = read_file("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq")
    hi = read_file("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    cur_max = read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
    return (
        int(lo) // 1000 if lo else 800,
        int(hi) // 1000 if hi else 5000,
        int(cur_max) // 1000 if cur_max else None,
    )


def boost_state():
    v = read_file("/sys/devices/system/cpu/cpufreq/boost")
    if v is not None:
        return v == "1"
    v = read_file("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if v is not None:
        return v == "0"
    v = read_file("/sys/devices/system/cpu/amd_pstate/no_turbo")
    if v is not None:
        return v == "0"
    return None


def intel_pstate_pct():
    return read_file("/sys/devices/system/cpu/intel_pstate/max_perf_pct")


def load_avg():
    out = read_file("/proc/loadavg", "0 0 0")
    parts = out.split()
    return parts[0], parts[1], parts[2]


def throttle_status():
    count = 0
    for f in glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count"):
        v = read_file(f, "0")
        try:
            count += int(v)
        except ValueError:
            pass
    return count


def cpu_temp_c():
    # Try hwmon first (no external dependency), fall back to `sensors`
    best = None
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read_file(os.path.join(hw, "name"), "")
        if name not in ("coretemp", "k10temp", "zenpower"):
            continue
        for inp in glob.glob(os.path.join(hw, "temp*_input")):
            v = read_file(inp)
            if v:
                try:
                    c = int(v) / 1000.0
                    if best is None or c > best:
                        best = c
                except ValueError:
                    pass
    if best is not None:
        return best
    try:
        out = subprocess.run(["sensors"], capture_output=True, text=True, timeout=2).stdout
        temps = re.findall(r"\+([\d.]+)\xb0C", out)
        if temps:
            return max(float(t) for t in temps)
    except Exception:
        pass
    return None


# ---- power draw --------------------------------------------------------
# Fix: on a lot of older Intel chips (roughly Sandy Bridge through early
# Skylake era) the RAPL "energy_uj" counter lives in a narrow hardware
# register and wraps around (resets to 0) every few dozen seconds under
# load. The old code just did energy_now - energy_prev, which goes
# *negative* right after a wrap and produced garbage/negative wattage.
# Fix here: read max_energy_range_uj (the wrap point) and, when a wrap is
# detected (new < old), add the distance back up to the top of the range
# before adding the new value. Newer chips have a huge range and basically
# never hit this, so the fix is a no-op for them.

def _rapl_zones():
    """All RAPL-style powercap zones - Intel and AMD both expose the same
    powercap sysfs shape, so one code path covers both vendors."""
    zones = []
    for base in sorted(glob.glob("/sys/class/powercap/intel-rapl:*")) + \
                sorted(glob.glob("/sys/class/powercap/amd-rapl:*")) + \
                sorted(glob.glob("/sys/class/powercap/amd_energy:*")):
        # only want the package-level zone (name like "intel-rapl:0", not
        # a sub-zone like "intel-rapl:0:0" for individual cores/uncore)
        if re.search(r"(intel-rapl|amd-rapl|amd_energy):\d+$", base):
            if os.path.exists(os.path.join(base, "energy_uj")):
                zones.append(base)
    return zones


_LAST_ENERGY = {}  # zone_path -> (energy_uj, timestamp)


def cpu_power_w():
    zones = _rapl_zones()
    if zones:
        path = zones[0]
        e_raw = read_file(os.path.join(path, "energy_uj"))
        if e_raw is not None:
            try:
                e = int(e_raw)
            except ValueError:
                e = None
            if e is not None:
                t = time.time()
                max_raw = read_file(os.path.join(path, "max_energy_range_uj"))
                try:
                    max_range = int(max_raw) if max_raw else None
                except ValueError:
                    max_range = None
                prev = _LAST_ENERGY.get(path)
                result = None
                if prev:
                    pe, pt = prev
                    de = e - pe
                    if de < 0:
                        # Counter wrapped (common on older Intel RAPL
                        # registers) - add the distance from pe up to the
                        # wrap point, then from 0 up to e.
                        if max_range:
                            de = (max_range - pe) + e
                        else:
                            de = None
                    dt = t - pt
                    if de is not None and dt > 0:
                        result = (de / 1_000_000) / dt
                _LAST_ENERGY[path] = (e, t)
                if result is not None and 0 <= result < 1000:
                    return result
    # Fallback: `sensors` output (covers boards where RAPL isn't exposed
    # via powercap, and some AMD PPT/"power1" readings)
    try:
        out = subprocess.run(["sensors"], capture_output=True, text=True, timeout=2).stdout
        m = re.search(r"(?:PPT|Package|power1|Vcore Power|CPU Power)\D+([\d.]+)\s*W", out, re.I)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


# ---- memory / RAM -------------------------------------------------------

def mem_info():
    """Returns dict with total/used/available/swap_total/swap_used, all in MB."""
    out = read_file("/proc/meminfo", "") or ""
    vals = {}
    for line in out.splitlines():
        m = re.match(r"(\w+):\s*(\d+)\s*kB", line)
        if m:
            vals[m.group(1)] = int(m.group(2))
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", 0)
    used = total - avail
    swap_total = vals.get("SwapTotal", 0)
    swap_free = vals.get("SwapFree", 0)
    swap_used = swap_total - swap_free
    cached = vals.get("Cached", 0) + vals.get("SReclaimable", 0)
    committed = vals.get("Committed_AS", 0)
    return {
        "total_mb": total // 1024,
        "used_mb": used // 1024,
        "available_mb": avail // 1024,
        "swap_total_mb": swap_total // 1024,
        "swap_used_mb": swap_used // 1024,
        "cached_mb": cached // 1024,
        "committed_mb": committed // 1024,
        "used_pct": round(100.0 * used / total, 1) if total else 0.0,
    }


def mem_pressure_pct():
    """Linux PSI (Pressure Stall Information) 'some' avg10 for memory, i.e.
    the % of the last 10s some task was stalled waiting on memory. Not
    available on older kernels - returns None instead of guessing."""
    out = read_file("/proc/pressure/memory")
    if not out:
        return None
    m = re.search(r"some avg10=([\d.]+)", out)
    return float(m.group(1)) if m else None


def top_mem_processes(limit=8):
    """Top RAM-consuming processes visible to this (non-root) user."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,rss,pmem,comm", "--sort=-rss"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines()[1:limit + 1]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, rss_kb, pmem, comm = parts
        try:
            rows.append((int(pid), int(rss_kb) // 1024, float(pmem), comm))
        except ValueError:
            continue
    return rows


def zswap_info():
    return {
        "enabled": read_file("/sys/module/zswap/parameters/enabled"),
        "compressor": read_file("/sys/module/zswap/parameters/compressor"),
        "pool_pct": read_file("/sys/module/zswap/parameters/max_pool_percent"),
        "available": os.path.isdir("/sys/module/zswap"),
    }


def available_compressors():
    out = read_file("/proc/crypto", "") or ""
    names = set(re.findall(r"name\s*:\s*(lz4hc|lz4|zstd|deflate|lzo-rle|lzo|842)", out))
    return sorted(names) if names else ["lz4", "zstd", "lzo", "deflate"]


def swapfile_present():
    try:
        out = subprocess.run(
            ["swapon", "--show=NAME,TYPE", "--noheadings"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return False
    return any(" file" in line or line.strip().endswith("file") for line in out.splitlines())


# ---- GPU (beta) ----------------------------------------------------------

def gpu_list():
    """Best-effort, read-only GPU detection via lspci. No root needed."""
    gpus = []
    try:
        out = subprocess.run(["lspci", "-k"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return gpus
    blocks = re.split(r"\n(?=\S)", out)
    for b in blocks:
        if "VGA compatible controller" not in b and "3D controller" not in b:
            continue
        first_line = b.splitlines()[0]
        name = first_line.split(": ", 1)[-1].strip() if ": " in first_line else first_line.strip()
        driver_m = re.search(r"Kernel driver in use:\s*(\S+)", b)
        driver = driver_m.group(1) if driver_m else "unknown"
        low = name.lower()
        if "nvidia" in low:
            vendor = "NVIDIA"
        elif re.search(r"\bamd\b|ati|radeon", low):
            vendor = "AMD"
        elif "intel" in low:
            vendor = "Intel"
        else:
            vendor = "Unknown"
        gpus.append({"name": name, "driver": driver, "vendor": vendor})
    return gpus


def gpu_vram_info():
    """VRAM totals from /sys/class/drm (works for most open-source KMS drivers)."""
    entries = []
    for total_f in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        used_f = total_f.replace("vram_total", "vram_used")
        total = read_file(total_f)
        used = read_file(used_f)
        try:
            total_mb = int(total) // (1024 * 1024) if total else None
            used_mb = int(used) // (1024 * 1024) if used else None
        except ValueError:
            total_mb = used_mb = None
        if total_mb:
            entries.append((total_f.split("/")[4], total_mb, used_mb))
    return entries


def nvidia_smi_info():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def gpu_codecs():
    """Video codec / profile support via vainfo (VAAPI), if installed."""
    try:
        out = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=3)
        text = out.stdout + out.stderr
    except Exception:
        return None
    profiles = sorted(set(re.findall(r"(VAProfile\w+)", text)))
    return profiles if profiles else None


def vulkan_summary():
    try:
        out = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


# ---- Drive (storage) ------------------------------------------------------

def list_drives():
    """Physical block devices via lsblk (read-only, no root needed)."""
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,MODEL,VENDOR,SIZE,TYPE,TRAN,ROTA,FSTYPE,MOUNTPOINT,SERIAL"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        data = json.loads(out)
    except Exception:
        return []
    drives = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        tran = (dev.get("tran") or "").lower()
        rota = dev.get("rota")
        if tran == "nvme":
            dtype = "NVMe SSD"
        elif tran == "usb":
            dtype = "USB storage"
        elif rota is False or rota == "0":
            dtype = "SATA SSD"
        elif rota is True or rota == "1":
            dtype = "HDD"
        else:
            dtype = "Unknown"
        parts = []
        for child in dev.get("children", []) or []:
            parts.append({
                "name": child.get("name"),
                "size": child.get("size") or 0,
                "fstype": child.get("fstype"),
                "mountpoint": child.get("mountpoint"),
            })
        drives.append({
            "name": dev.get("name"),
            "model": (dev.get("model") or "").strip() or "Unknown model",
            "vendor": (dev.get("vendor") or "").strip() or "Unknown",
            "size": dev.get("size") or 0,
            "type": dtype,
            "interface": tran.upper() or "unknown",
            "fstype": dev.get("fstype"),
            "mountpoint": dev.get("mountpoint"),
            "serial": dev.get("serial"),
            "partitions": parts,
        })
    return drives


def drive_usage(mountpoint):
    """Used/free/total for a mountpoint, in MB. None if not mounted."""
    if not mountpoint:
        return None
    try:
        st = os.statvfs(mountpoint)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        used = total - free
        return {
            "total_mb": total // (1024 * 1024),
            "used_mb": used // (1024 * 1024),
            "free_mb": free // (1024 * 1024),
        }
    except Exception:
        return None


def drive_temp_c(name):
    """Best-effort drive temp via hwmon (nvme) - no external tool needed."""
    for hw in glob.glob(f"/sys/class/block/{name}/device/hwmon*/temp1_input") + \
              glob.glob(f"/sys/class/nvme/{name}/hwmon*/temp1_input"):
        v = read_file(hw)
        if v:
            try:
                return int(v) / 1000.0
            except ValueError:
                pass
    return None


def drive_health(name):
    """SMART health summary via the root helper (smartctl needs raw device
    access most non-root users don't have). Returns a short string."""
    ok, msg = run_helper("drive_health", name)
    if ok and msg.startswith("OK:"):
        return msg[3:].strip()
    return None


_LAST_DISKSTATS = {}


def _read_diskstats():
    """name -> (sectors_read, sectors_written, ms_active) from /proc/diskstats.
    Sector size is always 512 bytes regardless of the drive's real block size
    (a long-standing kernel convention), so this scales correctly."""
    out = read_file("/proc/diskstats", "") or ""
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        try:
            sectors_read = int(parts[5])
            sectors_written = int(parts[9])
            ms_active = int(parts[12])
        except ValueError:
            continue
        result[name] = (sectors_read, sectors_written, ms_active)
    return result


def drive_activity(name):
    """Instantaneous read/write throughput (MB/s) and active-time % since
    the last call, for one device name (e.g. 'sda', 'nvme0n1')."""
    global _LAST_DISKSTATS
    cur = _read_diskstats()
    now = time.time()
    prev, prev_t = _LAST_DISKSTATS.get(name, (None, None))
    result = {"read_mb_s": 0.0, "write_mb_s": 0.0, "active_pct": 0.0}
    if name in cur:
        sr, sw, ms = cur[name]
        if prev:
            psr, psw, pms = prev
            dt = max(now - (prev_t or now), 0.001)
            result["read_mb_s"] = round(((sr - psr) * 512) / (1024 * 1024) / dt, 2)
            result["write_mb_s"] = round(((sw - psw) * 512) / (1024 * 1024) / dt, 2)
            result["active_pct"] = round(min(100.0, 100.0 * (ms - pms) / 1000.0 / dt), 1)
        _LAST_DISKSTATS[name] = ((sr, sw, ms), now)
    return result


def _rate_benchmark(size_mb):
    """Sequential write+read benchmark against a private temp file. Never
    touches user data: the file lives under tempfile's own dir, has a random
    name, and is always removed in a finally block."""
    size_bytes = size_mb * 1024 * 1024
    block = 4 * 1024 * 1024
    path = os.path.join(tempfile.gettempdir(), f"cpuctrl_bench_{uuid.uuid4().hex}.tmp")
    result = {"write_mb_s": None, "read_mb_s": None, "error": None}
    try:
        data = os.urandom(block)
        t0 = time.time()
        written = 0
        with open(path, "wb") as f:
            while written < size_bytes:
                f.write(data)
                written += block
            f.flush()
            os.fsync(f.fileno())
        dt = max(time.time() - t0, 0.001)
        result["write_mb_s"] = round((written / (1024 * 1024)) / dt, 1)

        # Drop OS page cache read of this file as best we can by reading
        # via O_DIRECT-ish large sequential reads; not guaranteed on every
        # filesystem, but the write timing above is unaffected either way.
        t0 = time.time()
        read_total = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(block)
                if not chunk:
                    break
                read_total += len(chunk)
        dt = max(time.time() - t0, 0.001)
        result["read_mb_s"] = round((read_total / (1024 * 1024)) / dt, 1)
    except PermissionError:
        result["error"] = "Permission denied writing temp benchmark file"
    except OSError as e:
        result["error"] = f"Disk error: {e}"
    except Exception as e:
        result["error"] = str(e)
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            logger.warning("Could not remove benchmark temp file %s", path)
    return result


def _random_io_benchmark(size_mb, block_kb=4):
    """Random 4K read/write IOPS + latency against the same kind of private
    temp file as _rate_benchmark, cleaned up the same way."""
    import random
    size_bytes = size_mb * 1024 * 1024
    block = block_kb * 1024
    n_blocks = max(size_bytes // block, 1)
    path = os.path.join(tempfile.gettempdir(), f"cpuctrl_bench_{uuid.uuid4().hex}.tmp")
    result = {"write_iops": None, "read_iops": None, "write_lat_ms": None, "read_lat_ms": None, "error": None}
    try:
        with open(path, "wb") as f:
            f.truncate(size_bytes)
        data = os.urandom(block)
        offsets = [random.randint(0, n_blocks - 1) * block for _ in range(min(n_blocks, 500))]

        t0 = time.time()
        with open(path, "r+b") as f:
            for off in offsets:
                f.seek(off)
                f.write(data)
            f.flush()
            os.fsync(f.fileno())
        dt = max(time.time() - t0, 0.001)
        result["write_iops"] = round(len(offsets) / dt)
        result["write_lat_ms"] = round((dt / len(offsets)) * 1000, 2)

        t0 = time.time()
        with open(path, "rb") as f:
            for off in offsets:
                f.seek(off)
                f.read(block)
        dt = max(time.time() - t0, 0.001)
        result["read_iops"] = round(len(offsets) / dt)
        result["read_lat_ms"] = round((dt / len(offsets)) * 1000, 2)
    except PermissionError:
        result["error"] = "Permission denied writing temp benchmark file"
    except OSError as e:
        result["error"] = f"Disk error: {e}"
    except Exception as e:
        result["error"] = str(e)
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            logger.warning("Could not remove benchmark temp file %s", path)
    return result


def rate_benchmark(size_mb):
    return safe_call(_rate_benchmark, size_mb, default={"write_mb_s": None, "read_mb_s": None, "error": "benchmark failed"})


def random_io_benchmark(size_mb):
    return safe_call(_random_io_benchmark, size_mb, default={"write_iops": None, "read_iops": None, "write_lat_ms": None, "read_lat_ms": None, "error": "benchmark failed"})


def performance_rating(read_mb_s, write_mb_s, is_ssd):
    """Rough Excellent/Good/Average/Slow rating - thresholds differ for
    SSD vs HDD since 150MB/s is great for a spinning disk and slow for SSD."""
    avg = ((read_mb_s or 0) + (write_mb_s or 0)) / 2
    if is_ssd:
        if avg >= 800:
            return "Excellent"
        if avg >= 350:
            return "Good"
        if avg >= 120:
            return "Average"
        return "Slow"
    else:
        if avg >= 180:
            return "Excellent"
        if avg >= 110:
            return "Good"
        if avg >= 60:
            return "Average"
        return "Slow"


_HELPER_PROC = None
_HELPER_LOCK = None  # set to threading.Lock() below, avoids import at module top twice


def _get_lock():
    global _HELPER_LOCK
    if _HELPER_LOCK is None:
        import threading
        _HELPER_LOCK = threading.Lock()
    return _HELPER_LOCK


def start_helper():
    """Launch ONE pkexec-authenticated root helper that stays alive for the
    whole session. This is the only time the user is asked for their
    password; every control action afterwards (CPU or RAM) is sent to this
    same process over a pipe, so no further prompts appear.
    Returns (ok, message).
    """
    global _HELPER_PROC
    try:
        _HELPER_PROC = subprocess.Popen(
            ["pkexec", HELPER, "daemon"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        _HELPER_PROC = None
        return False, "pkexec not found - install policykit-1"

    # Confirm the daemon actually came up authenticated (user may have
    # cancelled the password prompt, in which case pkexec exits fast).
    ok, msg = run_helper("ping")
    if not ok:
        _HELPER_PROC = None
        return False, msg or "Authorization was cancelled or failed"
    return True, "OK"


def stop_helper():
    global _HELPER_PROC
    if _HELPER_PROC and _HELPER_PROC.poll() is None:
        try:
            run_helper("exit")
        except Exception:
            pass
        try:
            _HELPER_PROC.terminate()
        except Exception:
            pass
    _HELPER_PROC = None


def _send_raw(action, value=""):
    """Send one action to the persistent helper and return the raw reply
    line, unparsed. Returns None if the helper isn't up / connection lost."""
    global _HELPER_PROC
    if _HELPER_PROC is None or _HELPER_PROC.poll() is not None:
        return None
    with _get_lock():
        try:
            line = f"{action} {value}".strip() + "\n"
            _HELPER_PROC.stdin.write(line)
            _HELPER_PROC.stdin.flush()
            return _HELPER_PROC.stdout.readline().strip()
        except Exception:
            return None


def run_helper(action, value=""):
    """Send one action to the already-authenticated persistent helper.
    Returns (ok, message). No password prompt happens here."""
    out = _send_raw(action, value)
    if out is None:
        return False, "Not authorized as root yet - restart the app"
    if out.startswith("OK"):
        return True, out
    return False, out or "Unknown error"


def ram_list_active():
    """Ask the helper for active RAM limits; returns a list of dicts.
    (This particular action replies with raw JSON, not an OK/ERROR line.)"""
    out = _send_raw("ram_list")
    if not out:
        return []
    try:
        return json.loads(out)
    except Exception:
        return []


def list_active_apps():
    """Names of currently-running user apps, filtered/cleaned the same way
    boost.sh did it: strip reverse-DNS flatpak junk, drop system/background
    noise, dedupe. Reused as one shell pipeline since that's exactly what
    boost.sh already got right."""
    real_user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    pipeline = (
        f"ps -u '{real_user}' -o args= | awk '{{print $1}}' | xargs -n 1 basename 2>/dev/null | "
        r"sed -E 's/^io\..*\.//i; s/^org\..*\.//i; s/^com\..*\.//i' | sort -u | "
        r"grep -vE '^(bash|sh|sudo|ps|sort|uniq|grep|tr|awk|sed|pr|head|xargs|basename|gnome-|systemd|"
        r"wayland|Xorg|pipewire|pulse|dbus|at-spi|gvfs|dconf|xdg|tracker|gdm|klipper|kscreen|plasma|"
        r"kwin|flatpak|snapd)' | head -n 60"
    )
    try:
        out = subprocess.run(["bash", "-c", pipeline], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def parse_freq_to_khz(text):
    text = text.strip().lower()
    if text in ("default", "max"):
        v = read_file("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        return int(v) if v else None
    m = re.match(r"^([\d.]+)\s*(ghz|mhz|khz)?$", text)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or "khz"
    if unit == "ghz":
        return int(num * 1_000_000)
    if unit == "mhz":
        return int(num * 1_000)
    return int(num)


# ---------------------------------------------------------------- UI

class StatBox(Gtk.Box):
    def __init__(self, label_text):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.label = Gtk.Label(label=label_text, xalign=0)
        self.label.get_style_context().add_class("stat-label")
        self.value = Gtk.Label(label="--", xalign=0)
        self.value.get_style_context().add_class("stat-value")
        self.pack_start(self.label, False, False, 0)
        self.pack_start(self.value, False, False, 0)

    def set_value(self, text, cls=None):
        self.value.set_text(text)
        ctx = self.value.get_style_context()
        for c in ("stat-value-green", "stat-value-cyan", "stat-value-orange", "stat-value-red", "stat-value-purple"):
            ctx.remove_class(c)
        if cls:
            ctx.add_class(cls)


def make_card(title, badge=None):
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    card.get_style_context().add_class("card")
    card.set_margin_top(8)
    card.set_margin_bottom(8)
    card.set_margin_start(8)
    card.set_margin_end(8)
    card.set_property("margin", 10)
    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    lbl = Gtk.Label(label=title, xalign=0)
    lbl.get_style_context().add_class("ctit")
    head.pack_start(lbl, True, True, 0)
    if badge:
        b = Gtk.Label(label=badge)
        b.get_style_context().add_class("beta-badge")
        head.pack_start(b, False, False, 0)
    card.pack_start(head, False, False, 0)
    return card


class CPUControlApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="CPU CONTROL v5.0")
        self.set_default_size(860, 720)
        self.set_border_width(0)
        self.connect("destroy", self._on_destroy)

        self.settings = load_settings()

        self.logging_active = False
        self.log_writer = None
        self.log_file = None
        self._tick_count = 0
        self._timeout_id = None

        # Bounded history buffers (ring buffers) - capped by settings so
        # they can never grow without bound across a long session.
        self._mem_history = deque(maxlen=self.settings["history_length"])
        self._mem_warning_shown = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        outer.pack_start(self._build_header(), False, False, 0)

        self.notebook = Gtk.Notebook()
        outer.pack_start(self.notebook, True, True, 0)

        self.notebook.append_page(self._build_dashboard(), Gtk.Label(label="DASHBOARD"))
        self.notebook.append_page(self._build_control(), Gtk.Label(label="CONTROL"))
        self.notebook.append_page(self._build_ram(), Gtk.Label(label="RAM"))
        self.notebook.append_page(self._build_boost(), Gtk.Label(label="BOOST"))
        self.notebook.append_page(self._build_drive(), Gtk.Label(label="DRIVE"))
        self.notebook.append_page(self._build_gpu(), Gtk.Label(label="GPU"))
        self.notebook.append_page(self._build_scan(), Gtk.Label(label="SCAN"))
        self.notebook.append_page(self._build_settings(), Gtk.Label(label="SETTINGS"))
        self.notebook.append_page(self._build_log(), Gtk.Label(label="LOGGING"))

        self._apply_refresh_interval()
        self._tick()

        if self.settings.get("start_minimized"):
            self.iconify()

    def _apply_refresh_interval(self):
        """(Re)installs the periodic tick at the interval implied by the
        current refresh_mode setting. Safe to call any time (e.g. right
        after the Settings tab changes it)."""
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        if not self.settings.get("background_monitoring", True):
            return
        ms = REFRESH_INTERVALS_MS.get(self.settings.get("refresh_mode", "balanced"), 1000)
        self._timeout_id = GLib.timeout_add(ms, self._tick)

    def _on_destroy(self, _win):
        stop_helper()
        save_settings(self.settings)
        Gtk.main_quit()

    # ---- header --------------------------------------------------
    def _build_header(self):
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hdr.set_border_width(10)
        hdr.get_style_context().add_class("card")

        logo = Gtk.Label()
        logo.set_markup('<span foreground="#ffffff">CPU</span><span foreground="#00ff88">CTRL</span>')
        logo.get_style_context().add_class("logo")
        hdr.pack_start(logo, False, False, 0)

        badge = Gtk.Label(label="v5.0")
        badge.get_style_context().add_class("vbadge")
        hdr.pack_start(badge, False, False, 0)

        vendor = cpu_vendor()
        vlabel = Gtk.Label(label=vendor.upper())
        vlabel.get_style_context().add_class("stat-value-purple")
        hdr.pack_start(vlabel, False, False, 0)

        self.model_label = Gtk.Label(label=cpu_model())
        self.model_label.get_style_context().add_class("stat-label")
        hdr.pack_start(self.model_label, True, True, 0)

        return hdr

    # ---- dashboard -------------------------------------------------
    def _build_dashboard(self):
        scroller = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(box)

        stats_card = make_card("LIVE STATUS")
        grid = Gtk.Grid(column_spacing=20, row_spacing=6)
        self.stat_power = StatBox("POWER DRAW")
        self.stat_temp = StatBox("TEMPERATURE")
        self.stat_thermal = StatBox("THERMAL STATE")
        self.stat_gov = StatBox("GOVERNOR")
        self.stat_load = StatBox("LOAD (1/5/15m)")
        self.stat_boost = StatBox("BOOST")
        for i, s in enumerate([self.stat_power, self.stat_temp, self.stat_thermal,
                                self.stat_gov, self.stat_load, self.stat_boost]):
            grid.attach(s, i % 3, i // 3, 1, 1)
        stats_card.pack_start(grid, False, False, 0)
        box.pack_start(stats_card, False, False, 0)

        cores_card = make_card("PER-CORE LOAD")
        self.core_grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        cores_card.pack_start(self.core_grid, False, False, 0)
        self.core_bars = []
        box.pack_start(cores_card, False, False, 0)

        # Fills the remaining space at the bottom of the dashboard.
        queue_card = make_card("WAITING FOR CPU")
        queue_card.set_vexpand(True)
        queue_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        queue_wrap.set_valign(Gtk.Align.CENTER)
        queue_wrap.set_vexpand(True)
        self.queue_value = Gtk.Label(label="0")
        self.queue_value.get_style_context().add_class("queue-big")
        self.queue_sub = Gtk.Label(label="apps ready to run but stuck waiting on a free core")
        self.queue_sub.get_style_context().add_class("hint-text")
        queue_wrap.pack_start(self.queue_value, False, False, 0)
        queue_wrap.pack_start(self.queue_sub, False, False, 0)
        queue_card.pack_start(queue_wrap, True, True, 0)
        box.pack_start(queue_card, True, True, 0)

        return scroller

    def _ensure_core_bars(self, n):
        if len(self.core_bars) == n:
            return
        for child in list(self.core_grid.get_children()):
            self.core_grid.remove(child)
        self.core_bars = []
        cols = 2
        for i in range(n):
            lbl = Gtk.Label(label=f"CORE {i}", xalign=0)
            lbl.get_style_context().add_class("core-label")
            bar = Gtk.ProgressBar()
            bar.set_show_text(True)
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.pack_start(lbl, False, False, 0)
            row_box.pack_start(bar, False, False, 0)
            self.core_grid.attach(row_box, i % cols, i // cols, 1, 1)
            self.core_bars.append(bar)
        self.core_grid.show_all()

    # ---- control -----------------------------------------------------
    def _build_control(self):
        scroller = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(box)

        gov_card = make_card("SCALING GOVERNOR")
        gov_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.gov_combo = Gtk.ComboBoxText()
        for g in available_governors():
            self.gov_combo.append_text(g)
        gov_apply = Gtk.Button(label="APPLY")
        gov_apply.get_style_context().add_class("accent")
        gov_apply.connect("clicked", self._on_apply_governor)
        gov_row.pack_start(self.gov_combo, True, True, 0)
        gov_row.pack_start(gov_apply, False, False, 0)
        gov_card.pack_start(gov_row, False, False, 0)
        box.pack_start(gov_card, False, False, 0)

        freq_card = make_card("MAX FREQUENCY LIMIT")
        freq_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.freq_entry = Gtk.Entry()
        self.freq_entry.set_placeholder_text("e.g. 3.5GHz, 3200MHz, or 'default'")
        freq_apply = Gtk.Button(label="APPLY")
        freq_apply.get_style_context().add_class("accent")
        freq_apply.connect("clicked", self._on_apply_freq)
        freq_row.pack_start(self.freq_entry, True, True, 0)
        freq_row.pack_start(freq_apply, False, False, 0)
        freq_card.pack_start(freq_row, False, False, 0)
        box.pack_start(freq_card, False, False, 0)

        boost_card = make_card("TURBO BOOST")
        boost_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.boost_switch = Gtk.Switch()
        self.boost_switch.connect("state-set", self._on_toggle_boost)
        boost_lbl = Gtk.Label(label="Enable boost / turbo")
        boost_row.pack_start(boost_lbl, False, False, 0)
        boost_row.pack_start(self.boost_switch, False, False, 0)
        boost_card.pack_start(boost_row, False, False, 0)
        box.pack_start(boost_card, False, False, 0)

        vendor = cpu_vendor()
        pstate_title = "MAX PERFORMANCE %" if vendor == "Unknown" else f"{vendor.upper()} P-STATE MAX PERFORMANCE %"
        pstate_card = make_card(pstate_title)
        pstate_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.pstate_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self.pstate_scale.set_value(100)
        self.pstate_scale.set_digits(0)
        pstate_apply = Gtk.Button(label="APPLY")
        pstate_apply.get_style_context().add_class("accent")
        pstate_apply.connect("clicked", self._on_apply_pstate)
        pstate_row.pack_start(self.pstate_scale, True, True, 0)
        pstate_row.pack_start(pstate_apply, False, False, 0)
        pstate_card.pack_start(pstate_row, False, False, 0)
        box.pack_start(pstate_card, False, False, 0)

        self.control_status = Gtk.Label(label="", xalign=0)
        self.control_status.get_style_context().add_class("hint-text")
        box.pack_start(self.control_status, False, False, 6)

        deps_card = make_card("DEPENDENCIES")
        deps_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        deps_btn = Gtk.Button(label="INSTALL / CHECK (lm-sensors, cpupower, jq, vainfo)")
        deps_btn.connect("clicked", self._on_install_deps)
        deps_row.pack_start(deps_btn, False, False, 0)
        deps_card.pack_start(deps_row, False, False, 0)
        box.pack_start(deps_card, False, False, 0)

        return scroller

    def _set_status(self, ok, msg):
        self.control_status.set_text(("[OK] " if ok else "[X] ") + msg)
        ctx = self.control_status.get_style_context()
        ctx.remove_class("hint-text")
        ctx.remove_class("stat-value-red")
        ctx.add_class("stat-value-green" if ok else "stat-value-red")

    def _on_apply_governor(self, _btn):
        g = self.gov_combo.get_active_text()
        if not g:
            return
        ok, msg = run_helper("governor", g)
        self._set_status(ok, f"Governor -> {g}" if ok else msg)

    def _on_apply_freq(self, _btn):
        khz = parse_freq_to_khz(self.freq_entry.get_text())
        if khz is None:
            self._set_status(False, "Invalid frequency format")
            return
        ok, msg = run_helper("freq_max", khz)
        self._set_status(ok, f"Max freq -> {khz} kHz" if ok else msg)

    def _on_toggle_boost(self, _sw, state):
        ok, msg = run_helper("boost", "1" if state else "0")
        self._set_status(ok, f"Boost {'enabled' if state else 'disabled'}" if ok else msg)
        return False

    def _on_apply_pstate(self, _btn):
        pct = int(self.pstate_scale.get_value())
        ok, msg = run_helper("pstate_max", pct)
        self._set_status(ok, f"Max perf -> {pct}%" if ok else msg)

    def _on_install_deps(self, _btn):
        ok, msg = run_helper("install_deps")
        self._set_status(ok, "Dependencies checked/installed" if ok else msg)

    # ---- RAM (merged from ramlimit2.0) --------------------------------
    def _build_ram(self):
        scroller = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(box)

        # -- memory overview --
        mem_card = make_card("MEMORY OVERVIEW")
        grid = Gtk.Grid(column_spacing=20, row_spacing=6)
        self.ram_stat_total = StatBox("TOTAL RAM")
        self.ram_stat_used = StatBox("USED")
        self.ram_stat_avail = StatBox("AVAILABLE")
        self.ram_stat_swaptotal = StatBox("SWAP TOTAL")
        self.ram_stat_swapused = StatBox("SWAP USED")
        self.ram_stat_cached = StatBox("CACHED")
        self.ram_stat_committed = StatBox("COMMITTED")
        self.ram_stat_pressure = StatBox("MEM PRESSURE")
        for i, s in enumerate([self.ram_stat_total, self.ram_stat_used, self.ram_stat_avail,
                                self.ram_stat_swaptotal, self.ram_stat_swapused, self.ram_stat_cached,
                                self.ram_stat_committed, self.ram_stat_pressure]):
            grid.attach(s, i % 3, i // 3, 1, 1)
        mem_card.pack_start(grid, False, False, 0)
        self.ram_warning = Gtk.Label(label="", xalign=0)
        self.ram_warning.set_line_wrap(True)
        self.ram_warning.get_style_context().add_class("warn-text")
        self.ram_warning.set_no_show_all(True)
        mem_card.pack_start(self.ram_warning, False, False, 0)
        box.pack_start(mem_card, False, False, 0)

        # -- top consumers --
        top_card = make_card("TOP MEMORY CONSUMERS")
        self.ram_top_view = Gtk.TextView()
        self.ram_top_view.set_editable(False)
        self.ram_top_view.set_monospace(True)
        top_sw = Gtk.ScrolledWindow()
        top_sw.add(self.ram_top_view)
        top_sw.set_min_content_height(160)
        top_card.pack_start(top_sw, False, False, 0)
        box.pack_start(top_card, False, False, 0)

        # -- limit a process --
        limit_card = make_card("LIMIT A PROCESS")
        limit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.ram_pid_entry = Gtk.Entry()
        self.ram_pid_entry.set_placeholder_text("PID")
        self.ram_pid_entry.set_width_chars(8)
        self.ram_limit_entry = Gtk.Entry()
        self.ram_limit_entry.set_placeholder_text("Limit (MB)")
        self.ram_mode_combo = Gtk.ComboBoxText()
        self.ram_mode_combo.append_text("Push to Swap")
        self.ram_mode_combo.append_text("Hard Limit (no swap)")
        self.ram_mode_combo.set_active(0)
        ram_limit_btn = Gtk.Button(label="APPLY LIMIT")
        ram_limit_btn.get_style_context().add_class("accent")
        ram_limit_btn.connect("clicked", self._on_ram_apply_limit)
        limit_row.pack_start(self.ram_pid_entry, False, False, 0)
        limit_row.pack_start(self.ram_limit_entry, False, False, 0)
        limit_row.pack_start(self.ram_mode_combo, False, False, 0)
        limit_row.pack_start(ram_limit_btn, False, False, 0)
        limit_card.pack_start(limit_row, False, False, 0)
        hint = Gtk.Label(label="Push to Swap: excess RAM spills to swap, process keeps running.  "
                                "Hard Limit: no swap allowed, process is OOM-killed if it goes over.",
                          xalign=0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("hint-text")
        limit_card.pack_start(hint, False, False, 0)
        self.ram_status = Gtk.Label(label="", xalign=0)
        self.ram_status.get_style_context().add_class("hint-text")
        limit_card.pack_start(self.ram_status, False, False, 0)
        box.pack_start(limit_card, False, False, 0)

        # -- active limits --
        active_card = make_card("ACTIVE LIMITS")
        self.ram_active_view = Gtk.TextView()
        self.ram_active_view.set_editable(False)
        self.ram_active_view.set_monospace(True)
        active_sw = Gtk.ScrolledWindow()
        active_sw.add(self.ram_active_view)
        active_sw.set_min_content_height(140)
        active_card.pack_start(active_sw, False, False, 0)
        active_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.ram_clear_pid_entry = Gtk.Entry()
        self.ram_clear_pid_entry.set_placeholder_text("PID to clear")
        self.ram_clear_pid_entry.set_width_chars(10)
        clear_one_btn = Gtk.Button(label="CLEAR PID")
        clear_one_btn.get_style_context().add_class("info")
        clear_one_btn.connect("clicked", self._on_ram_clear_one)
        clear_all_btn = Gtk.Button(label="CLEAR ALL LIMITS")
        clear_all_btn.get_style_context().add_class("danger")
        clear_all_btn.connect("clicked", self._on_ram_clear_all)
        refresh_btn = Gtk.Button(label="REFRESH")
        refresh_btn.connect("clicked", lambda b: self._refresh_ram_active())
        active_btn_row.pack_start(self.ram_clear_pid_entry, False, False, 0)
        active_btn_row.pack_start(clear_one_btn, False, False, 0)
        active_btn_row.pack_start(clear_all_btn, False, False, 0)
        active_btn_row.pack_start(refresh_btn, False, False, 0)
        active_card.pack_start(active_btn_row, False, False, 0)
        box.pack_start(active_card, False, False, 0)

        # -- zswap --
        zswap_card = make_card("ZSWAP (COMPRESSED RAM CACHE)")
        zrow1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.zswap_switch = Gtk.Switch()
        self.zswap_switch.connect("state-set", self._on_zswap_toggle)
        z_lbl = Gtk.Label(label="zswap enabled")
        zrow1.pack_start(z_lbl, False, False, 0)
        zrow1.pack_start(self.zswap_switch, False, False, 0)
        zswap_card.pack_start(zrow1, False, False, 0)

        zrow2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.zswap_compressor_combo = Gtk.ComboBoxText()
        for c in available_compressors():
            self.zswap_compressor_combo.append_text(c)
        comp_apply = Gtk.Button(label="SET COMPRESSOR")
        comp_apply.get_style_context().add_class("accent")
        comp_apply.connect("clicked", self._on_zswap_compressor)
        zrow2.pack_start(self.zswap_compressor_combo, True, True, 0)
        zrow2.pack_start(comp_apply, False, False, 0)
        zswap_card.pack_start(zrow2, False, False, 0)

        zrow3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.zswap_pool_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 90, 1)
        self.zswap_pool_scale.set_value(20)
        self.zswap_pool_scale.set_digits(0)
        pool_apply = Gtk.Button(label="SET POOL %")
        pool_apply.get_style_context().add_class("accent")
        pool_apply.connect("clicked", self._on_zswap_pool)
        zrow3.pack_start(self.zswap_pool_scale, True, True, 0)
        zrow3.pack_start(pool_apply, False, False, 0)
        zswap_card.pack_start(zrow3, False, False, 0)

        self.zswap_status = Gtk.Label(label="", xalign=0)
        self.zswap_status.get_style_context().add_class("hint-text")
        zswap_card.pack_start(self.zswap_status, False, False, 0)
        box.pack_start(zswap_card, False, False, 0)

        # -- swap strategy (disk vs zswap ratio) --
        ratio_card = make_card("SWAP STRATEGY - DISK / ZSWAP RATIO")
        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for label, disk, zsw in [("FAVOR DISK 70/30", 70, 30), ("BALANCED 50/50", 50, 50),
                                  ("FAVOR ZSWAP 30/70", 30, 70), ("MAX MEMORY 10/90", 10, 90)]:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self._on_ram_ratio_preset, disk, zsw)
            preset_row.pack_start(btn, True, True, 0)
        ratio_card.pack_start(preset_row, False, False, 0)

        custom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.ratio_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.ratio_scale.set_value(50)
        custom_lbl = Gtk.Label(label="Disk %")
        custom_lbl.get_style_context().add_class("hint-text")
        custom_apply = Gtk.Button(label="APPLY CUSTOM")
        custom_apply.get_style_context().add_class("accent")
        custom_apply.connect("clicked", self._on_ram_ratio_custom)
        custom_row.pack_start(custom_lbl, False, False, 0)
        custom_row.pack_start(self.ratio_scale, True, True, 0)
        custom_row.pack_start(custom_apply, False, False, 0)
        ratio_card.pack_start(custom_row, False, False, 0)

        if not swapfile_present():
            warn = Gtk.Label(label="No file-backed swap detected - the disk portion of this ratio "
                                    "won't have anything to resize until one exists.", xalign=0)
            warn.set_line_wrap(True)
            warn.get_style_context().add_class("warn-text")
            ratio_card.pack_start(warn, False, False, 0)

        self.ratio_status = Gtk.Label(label="", xalign=0)
        self.ratio_status.get_style_context().add_class("hint-text")
        ratio_card.pack_start(self.ratio_status, False, False, 0)
        box.pack_start(ratio_card, False, False, 0)

        self._refresh_ram_active()
        return scroller

    def _on_ram_apply_limit(self, _btn):
        pid_text = self.ram_pid_entry.get_text().strip()
        limit_text = self.ram_limit_entry.get_text().strip()
        if not pid_text.isdigit() or not limit_text.isdigit():
            self.ram_status.set_text("[X] PID and limit must be numbers")
            return
        mode = "hard" if self.ram_mode_combo.get_active() == 1 else "swap"
        ok, msg = run_helper("ram_limit", f"{pid_text} {limit_text} {mode}")
        self.ram_status.set_text(("[OK] " if ok else "[X] ") +
                                  (f"Limited PID {pid_text} to {limit_text}MB ({mode})" if ok else msg))
        self._refresh_ram_active()

    def _on_ram_clear_one(self, _btn):
        pid_text = self.ram_clear_pid_entry.get_text().strip()
        if not pid_text.isdigit():
            self.ram_status.set_text("[X] Enter a numeric PID to clear")
            return
        ok, msg = run_helper("ram_clear", pid_text)
        self.ram_status.set_text(("[OK] " if ok else "[X] ") + (f"Cleared PID {pid_text}" if ok else msg))
        self._refresh_ram_active()

    def _on_ram_clear_all(self, _btn):
        ok, msg = run_helper("ram_clear_all")
        self.ram_status.set_text(("[OK] " if ok else "[X] ") + ("All limits cleared" if ok else msg))
        self._refresh_ram_active()

    def _refresh_ram_active(self):
        entries = ram_list_active()
        buf = self.ram_active_view.get_buffer()
        if not entries:
            buf.set_text("No active RAM limits.")
            return
        lines = [f"{'PID':<8}{'CMD':<18}{'LIMIT MB':<10}{'CURRENT MB':<12}{'MODE':<14}{'STATUS'}"]
        for e in entries:
            status = "DEAD" if not e.get("alive") else ("OVER" if e.get("current_mb", 0) > e.get("limit_mb", 0) else "OK")
            mode_label = "Hard (no swap)" if e.get("mode") == "hard" else "Push to Swap"
            lines.append(f"{e.get('pid',''):<8}{e.get('cmd',''):<18}{e.get('limit_mb',''):<10}"
                         f"{e.get('current_mb',''):<12}{mode_label:<14}{status}")
        buf.set_text("\n".join(lines))

    def _on_zswap_toggle(self, _sw, state):
        ok, msg = run_helper("zswap_set", f"enabled {1 if state else 0}")
        self.zswap_status.set_text(("[OK] " if ok else "[X] ") +
                                    (f"zswap {'enabled' if state else 'disabled'}" if ok else msg))
        return False

    def _on_zswap_compressor(self, _btn):
        c = self.zswap_compressor_combo.get_active_text()
        if not c:
            return
        ok, msg = run_helper("zswap_set", f"compressor {c}")
        self.zswap_status.set_text(("[OK] " if ok else "[X] ") + (f"Compressor -> {c}" if ok else msg))

    def _on_zswap_pool(self, _btn):
        pct = int(self.zswap_pool_scale.get_value())
        ok, msg = run_helper("zswap_set", f"pool {pct}")
        self.zswap_status.set_text(("[OK] " if ok else "[X] ") + (f"Pool -> {pct}%" if ok else msg))

    def _confirm_ratio_change(self, disk, zsw):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Apply swap ratio {disk}% disk / {zsw}% zswap?",
        )
        dlg.format_secondary_text(
            "This will briefly swapoff/resize/swapon your file-backed swap "
            "(if one exists) and update zswap's pool percent. Swap is "
            "unavailable for a moment during the resize."
        )
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.YES

    def _on_ram_ratio_preset(self, _btn, disk, zsw):
        if not self._confirm_ratio_change(disk, zsw):
            return
        ok, msg = run_helper("swap_ratio", f"{disk} {zsw}")
        self.ratio_status.set_text(("[OK] " if ok else "[X] ") +
                                    (f"Ratio -> {disk}% disk / {zsw}% zswap" if ok else msg))

    def _on_ram_ratio_custom(self, _btn):
        disk = int(self.ratio_scale.get_value())
        zsw = 100 - disk
        if not self._confirm_ratio_change(disk, zsw):
            return
        ok, msg = run_helper("swap_ratio", f"{disk} {zsw}")
        self.ratio_status.set_text(("[OK] " if ok else "[X] ") +
                                    (f"Ratio -> {disk}% disk / {zsw}% zswap" if ok else msg))

    # ---- Boost (merged from boost.sh) --------------------------------
    def _build_boost(self):
        scroller = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(box)

        apps_card = make_card("ACTIVE APPS RUNNING RIGHT NOW")
        self.boost_apps_view = Gtk.TextView()
        self.boost_apps_view.set_editable(False)
        self.boost_apps_view.set_monospace(True)
        apps_sw = Gtk.ScrolledWindow()
        apps_sw.add(self.boost_apps_view)
        apps_sw.set_min_content_height(180)
        apps_card.pack_start(apps_sw, True, True, 0)
        refresh_apps_btn = Gtk.Button(label="REFRESH APP LIST")
        refresh_apps_btn.connect("clicked", lambda b: self._refresh_boost_apps())
        apps_card.pack_start(refresh_apps_btn, False, False, 0)
        box.pack_start(apps_card, False, False, 0)

        boost_card = make_card("BOOST AN APP")
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_lbl = Gtk.Label(label="App name (as shown above):", xalign=0)
        self.boost_app_entry = Gtk.Entry()
        self.boost_app_entry.set_placeholder_text("e.g. firefox, steam, MissionCenter")
        name_row.pack_start(name_lbl, False, False, 0)
        name_row.pack_start(self.boost_app_entry, True, True, 0)
        boost_card.pack_start(name_row, False, False, 0)

        pr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pr_lbl = Gtk.Label(label="Priority (-20 highest \u2192 19 lowest):", xalign=0)
        self.boost_priority_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -20, 19, 1)
        self.boost_priority_scale.set_value(-14)
        self.boost_priority_scale.set_digits(0)
        pr_row.pack_start(pr_lbl, False, False, 0)
        pr_row.pack_start(self.boost_priority_scale, True, True, 0)
        boost_card.pack_start(pr_row, False, False, 0)

        boost_btn = Gtk.Button(label="BOOST APP")
        boost_btn.get_style_context().add_class("accent")
        boost_btn.connect("clicked", self._on_boost_app)
        boost_card.pack_start(boost_btn, False, False, 0)

        self.boost_status = Gtk.Label(label="", xalign=0)
        self.boost_status.set_line_wrap(True)
        self.boost_status.get_style_context().add_class("hint-text")
        boost_card.pack_start(self.boost_status, False, False, 0)
        box.pack_start(boost_card, False, False, 0)

        self._refresh_boost_apps()
        return scroller

    def _refresh_boost_apps(self):
        apps = safe_call(list_active_apps, default=[])
        buf = self.boost_apps_view.get_buffer()
        if not apps:
            buf.set_text("No apps detected (or ps/awk not available).")
            return
        # 4-column layout, same spirit as boost.sh's `pr -4 -t -w 58`
        cols = 4
        width = 18
        rows = []
        for i in range(0, len(apps), cols):
            row = apps[i:i + cols]
            rows.append("".join(a.ljust(width) for a in row))
        buf.set_text("\n".join(rows))

    def _on_boost_app(self, _btn):
        app_name = self.boost_app_entry.get_text().strip()
        if not app_name:
            self.boost_status.set_text("[X] Enter an app name first")
            return
        priority = int(self.boost_priority_scale.get_value())
        ok, msg = run_helper("proc_boost", f"{priority} {app_name}")
        self.boost_status.set_text(("[OK] " if ok else "[X] ") + (msg[3:].strip() if ok and msg.startswith("OK:") else msg))

    # ---- Drive -----------------------------------------------------------
    def _build_drive(self):
        scroller = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(box)

        info_card = make_card("DRIVES")
        self.drive_info_view = Gtk.TextView()
        self.drive_info_view.set_editable(False)
        self.drive_info_view.set_monospace(True)
        info_sw = Gtk.ScrolledWindow()
        info_sw.add(self.drive_info_view)
        info_sw.set_min_content_height(220)
        info_card.pack_start(info_sw, True, True, 0)
        refresh_info_btn = Gtk.Button(label="REFRESH DRIVE INFO")
        refresh_info_btn.get_style_context().add_class("accent")
        refresh_info_btn.connect("clicked", lambda b: self._refresh_drive_info())
        info_card.pack_start(refresh_info_btn, False, False, 0)
        box.pack_start(info_card, False, False, 0)

        act_card = make_card("DRIVE ACTIVITY")
        grid = Gtk.Grid(column_spacing=20, row_spacing=6)
        self.drive_stat_read = StatBox("READ SPEED")
        self.drive_stat_write = StatBox("WRITE SPEED")
        self.drive_stat_active = StatBox("ACTIVE TIME")
        for i, s in enumerate([self.drive_stat_read, self.drive_stat_write, self.drive_stat_active]):
            grid.attach(s, i, 0, 1, 1)
        act_card.pack_start(grid, False, False, 0)

        act_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        act_lbl = Gtk.Label(label="Watching:", xalign=0)
        act_lbl.get_style_context().add_class("hint-text")
        self.drive_activity_combo = Gtk.ComboBoxText()
        act_row.pack_start(act_lbl, False, False, 0)
        act_row.pack_start(self.drive_activity_combo, False, False, 0)
        act_card.pack_start(act_row, False, False, 0)
        box.pack_start(act_card, False, False, 0)

        bench_card = make_card("DRIVE BENCHMARK")
        brow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.bench_drive_combo = Gtk.ComboBoxText()
        self.bench_test_combo = Gtk.ComboBoxText()
        for t in ["Sequential Read", "Sequential Write", "Sequential Read + Write",
                  "Random Read", "Random Write", "Full Benchmark"]:
            self.bench_test_combo.append_text(t)
        self.bench_test_combo.set_active(2)
        bench_btn = Gtk.Button(label="RUN BENCHMARK")
        bench_btn.get_style_context().add_class("accent")
        bench_btn.connect("clicked", self._on_run_benchmark)
        brow.pack_start(self.bench_drive_combo, True, True, 0)
        brow.pack_start(self.bench_test_combo, True, True, 0)
        brow.pack_start(bench_btn, False, False, 0)
        bench_card.pack_start(brow, False, False, 0)

        self.bench_progress = Gtk.ProgressBar()
        self.bench_progress.set_show_text(True)
        self.bench_progress.set_text("Idle")
        bench_card.pack_start(self.bench_progress, False, False, 0)

        warn = Gtk.Label(
            label="Benchmarks only ever write to a private temp file that's deleted "
                  "automatically when the test finishes - your files are never touched.",
            xalign=0)
        warn.set_line_wrap(True)
        warn.get_style_context().add_class("hint-text")
        bench_card.pack_start(warn, False, False, 0)

        self.bench_results_view = Gtk.TextView()
        self.bench_results_view.set_editable(False)
        self.bench_results_view.set_monospace(True)
        bres_sw = Gtk.ScrolledWindow()
        bres_sw.add(self.bench_results_view)
        bres_sw.set_min_content_height(140)
        bench_card.pack_start(bres_sw, True, True, 0)
        box.pack_start(bench_card, False, False, 0)

        self._drives_cache = []
        self._refresh_drive_info()
        return scroller

    def _refresh_drive_info(self):
        drives = safe_call(list_drives, default=[])
        self._drives_cache = drives

        self.drive_activity_combo.remove_all()
        self.bench_drive_combo.remove_all()
        for d in drives:
            self.drive_activity_combo.append_text(d["name"])
            self.bench_drive_combo.append_text(d["name"])
        if drives:
            self.drive_activity_combo.set_active(0)
            self.bench_drive_combo.set_active(0)

        lines = ["========== DRIVES =========="]
        if not drives:
            lines.append("No drives detected (lsblk missing or returned nothing).")
        for d in drives:
            lines.append("")
            lines.append(f"/dev/{d['name']}  -  {d['model']} ({d['vendor']})")
            lines.append(f"  Type: {d['type']}   Interface: {d['interface'] or 'unknown'}")
            lines.append(f"  Capacity: {d['size'] / (1024**3):.1f} GB")
            if d["serial"]:
                lines.append(f"  Serial: {d['serial']}")
            temp = safe_call(drive_temp_c, d["name"])
            lines.append(f"  Temperature: {f'{temp:.0f}\u00b0C' if temp is not None else 'N/A'}")
            health = safe_call(drive_health, d["name"])
            lines.append(f"  Health (SMART): {health or 'N/A (needs smartctl + root helper)'}")
            if d["fstype"]:
                lines.append(f"  Filesystem: {d['fstype']}   Mount: {d['mountpoint'] or '(not mounted)'}")
                u = safe_call(drive_usage, d["mountpoint"])
                if u:
                    lines.append(f"  Used: {u['used_mb']} MB / {u['total_mb']} MB  (Free: {u['free_mb']} MB)")
            for p in d["partitions"]:
                lines.append(f"  \u2514 {p['name']}: {p['size']/(1024**3):.1f} GB  "
                             f"{p['fstype'] or ''}  {p['mountpoint'] or ''}")
        buf = self.drive_info_view.get_buffer()
        buf.set_text("\n".join(lines))

    def _drive_refresh_activity(self):
        name = self.drive_activity_combo.get_active_text()
        if not name:
            return
        a = safe_call(drive_activity, name, default={"read_mb_s": 0.0, "write_mb_s": 0.0, "active_pct": 0.0})
        self.drive_stat_read.set_value(f"{a['read_mb_s']} MB/s", "stat-value-cyan")
        self.drive_stat_write.set_value(f"{a['write_mb_s']} MB/s", "stat-value-orange")
        act_cls = "stat-value-red" if a["active_pct"] > 80 else ("stat-value-orange" if a["active_pct"] > 40 else "stat-value-green")
        self.drive_stat_active.set_value(f"{a['active_pct']}%", act_cls)

    def _on_run_benchmark(self, _btn):
        drive = self.bench_drive_combo.get_active_text()
        test = self.bench_test_combo.get_active_text()
        if not drive or not test:
            return
        if self.settings.get("confirm_before_benchmark", True):
            dlg = Gtk.MessageDialog(
                transient_for=self, flags=0, message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text=f"Run '{test}' on {drive}?",
            )
            size_mb = self.settings.get("benchmark_size_mb", 256)
            dlg.format_secondary_text(
                f"Uses a private {size_mb} MB temp file, deleted automatically when done. "
                f"Heavy tests can briefly affect other disk activity."
            )
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return

        self.bench_progress.set_fraction(0.1)
        self.bench_progress.set_text(f"Running: {test}...")
        # Runs synchronously (benchmarks are inherently blocking I/O); GTK
        # repaints the progress bar first via this pending-events flush so
        # the UI doesn't look frozen at 0% while the test runs.
        while Gtk.events_pending():
            Gtk.main_iteration()

        size_mb = self.settings.get("benchmark_size_mb", 256)
        is_ssd = "SSD" in next((d["type"] for d in self._drives_cache if d["name"] == drive), "")
        lines = [f"========== BENCHMARK: {drive} - {test} =========="]
        results = {}

        if test in ("Sequential Read", "Sequential Write", "Sequential Read + Write", "Full Benchmark"):
            r = rate_benchmark(size_mb)
            results.update(r)
        if test in ("Random Read", "Random Write", "Full Benchmark"):
            r2 = random_io_benchmark(size_mb)
            results.update(r2)

        self.bench_progress.set_fraction(1.0)

        if results.get("error"):
            self.bench_progress.set_text("Failed")
            lines.append(f"Error: {results['error']}")
        else:
            self.bench_progress.set_text("Complete")
            if results.get("write_mb_s") is not None:
                lines.append(f"Sequential Write: {results['write_mb_s']} MB/s")
            if results.get("read_mb_s") is not None:
                lines.append(f"Sequential Read:  {results['read_mb_s']} MB/s")
            if results.get("write_iops") is not None:
                lines.append(f"Random Write IOPS: {results['write_iops']}  (latency {results['write_lat_ms']} ms)")
            if results.get("read_iops") is not None:
                lines.append(f"Random Read IOPS:  {results['read_iops']}  (latency {results['read_lat_ms']} ms)")
            if results.get("write_mb_s") is not None or results.get("read_mb_s") is not None:
                rating = performance_rating(results.get("read_mb_s"), results.get("write_mb_s"), is_ssd)
                lines.append(f"Performance rating: {rating}")
            lines.append(f"Test size: {size_mb} MB")

        buf = self.bench_results_view.get_buffer()
        buf.set_text("\n".join(lines))

    # ---- Settings ----------------------------------------------------
    def _build_settings(self):
        scroller = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(box)

        gen_card = make_card("GENERAL")
        self.set_theme_combo = self._settings_combo(gen_card, "Theme", ["dark", "light", "system"], "theme")
        self.set_scale_spin = self._settings_spin(gen_card, "UI Scaling %", 75, 150, "ui_scale")
        self.set_start_min_sw = self._settings_switch(gen_card, "Start minimized", "start_minimized")
        self.set_start_sys_sw = self._settings_switch(gen_card, "Start with system", "start_with_system")
        box.pack_start(gen_card, False, False, 0)

        mon_card = make_card("MONITORING")
        self.set_refresh_combo = self._settings_combo(mon_card, "Refresh rate",
                                                        ["fast", "balanced", "low_resource"], "refresh_mode")
        self.set_bgmon_sw = self._settings_switch(mon_card, "Background monitoring", "background_monitoring")
        self.set_notif_sw = self._settings_switch(mon_card, "Notifications", "notifications_enabled")
        self.set_history_spin = self._settings_spin(mon_card, "History length (samples)", 30, 1000, "history_length")
        box.pack_start(mon_card, False, False, 0)

        mem_card = make_card("MEMORY")
        self.set_memwarn_spin = self._settings_spin(mem_card, "Memory warning threshold %", 50, 99, "mem_warning_pct")
        self.set_lowres_sw = self._settings_switch(mem_card, "Low resource mode", "low_resource_mode")
        clear_cache_btn = Gtk.Button(label="CLEAR CACHED APPLICATION DATA")
        clear_cache_btn.connect("clicked", self._on_clear_app_cache)
        mem_card.pack_start(clear_cache_btn, False, False, 4)
        box.pack_start(mem_card, False, False, 0)

        drive_card = make_card("DRIVE")
        self.set_benchsize_spin = self._settings_spin(drive_card, "Default benchmark size (MB)", 16, 4096, "benchmark_size_mb")
        self.set_benchdur_spin = self._settings_spin(drive_card, "Default benchmark duration (s)", 1, 120, "benchmark_duration_s")
        self.set_confirmbench_sw = self._settings_switch(drive_card, "Confirm before running benchmark", "confirm_before_benchmark")
        self.set_autoclean_sw = self._settings_switch(drive_card, "Auto-clean benchmark files", "auto_clean_benchmark_files")
        box.pack_start(drive_card, False, False, 0)

        adv_card = make_card("ADVANCED")
        self.set_debug_sw = self._settings_switch(adv_card, "Debug logging", "logging_debug")
        adv_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        export_btn = Gtk.Button(label="EXPORT DIAGNOSTICS")
        export_btn.connect("clicked", self._on_export_diagnostics)
        reset_btn = Gtk.Button(label="RESET ALL SETTINGS")
        reset_btn.get_style_context().add_class("danger")
        reset_btn.connect("clicked", self._on_reset_settings)
        adv_row.pack_start(export_btn, False, False, 0)
        adv_row.pack_start(reset_btn, False, False, 0)
        adv_card.pack_start(adv_row, False, False, 0)
        box.pack_start(adv_card, False, False, 0)

        self.settings_status = Gtk.Label(label="Settings apply immediately and are saved automatically.", xalign=0)
        self.settings_status.get_style_context().add_class("hint-text")
        box.pack_start(self.settings_status, False, False, 6)

        return scroller

    def _settings_switch(self, card, label_text, key):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label_text, xalign=0)
        sw = Gtk.Switch()
        sw.set_active(bool(self.settings.get(key, DEFAULT_SETTINGS[key])))
        sw.connect("state-set", lambda s, state, k=key: self._on_setting_changed(k, state))
        row.pack_start(lbl, True, True, 0)
        row.pack_start(sw, False, False, 0)
        card.pack_start(row, False, False, 0)
        return sw

    def _settings_combo(self, card, label_text, options, key):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label_text, xalign=0)
        combo = Gtk.ComboBoxText()
        for o in options:
            combo.append_text(o)
        cur = self.settings.get(key, DEFAULT_SETTINGS[key])
        combo.set_active(options.index(cur) if cur in options else 0)
        combo.connect("changed", lambda c, k=key: self._on_setting_changed(k, c.get_active_text()))
        row.pack_start(lbl, True, True, 0)
        row.pack_start(combo, False, False, 0)
        card.pack_start(row, False, False, 0)
        return combo

    def _settings_spin(self, card, label_text, lo, hi, key):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label_text, xalign=0)
        adj = Gtk.Adjustment(value=self.settings.get(key, DEFAULT_SETTINGS[key]), lower=lo, upper=hi, step_increment=1)
        spin = Gtk.SpinButton(adjustment=adj)
        spin.connect("value-changed", lambda s, k=key: self._on_setting_changed(k, int(s.get_value())))
        row.pack_start(lbl, True, True, 0)
        row.pack_start(spin, False, False, 0)
        card.pack_start(row, False, False, 0)
        return spin

    def _on_setting_changed(self, key, value):
        if key == "refresh_mode" and value not in REFRESH_INTERVALS_MS:
            return False
        if key == "mem_warning_pct" and not (1 <= int(value) <= 99):
            return False
        self.settings[key] = value
        save_settings(self.settings)
        self.settings_status.set_text(f"Saved: {key} = {value}")

        if key in ("refresh_mode", "background_monitoring"):
            self._apply_refresh_interval()
        if key == "history_length":
            self._mem_history = deque(self._mem_history, maxlen=int(value))
        if key == "logging_debug":
            logger.setLevel(logging.DEBUG if value else logging.INFO)
        return False

    def _on_clear_app_cache(self, _btn):
        self._mem_history.clear()
        _LAST_ENERGY.clear()
        _LAST_DISKSTATS.clear()
        self.settings_status.set_text("Cleared in-memory cached application data.")

    def _on_export_diagnostics(self, _btn):
        out_path = os.path.join(os.path.expanduser("~"), "cpu_control_diagnostics.txt")

        def _write():
            with open(out_path, "w") as f:
                f.write(f"CPU Control v5.0 diagnostics - {datetime.now().isoformat()}\n\n")
                f.write(f"CPU: {cpu_model()} ({cpu_vendor()})\n")
                f.write(f"Settings: {json.dumps(self.settings, indent=2)}\n\n")
                f.write("---- recent log ----\n")
                if os.path.exists(LOG_FILE):
                    f.write(read_file(LOG_FILE, "") or "")
            return True

        ok = safe_call(_write, default=False)
        self.settings_status.set_text(f"Diagnostics exported to {out_path}" if ok else "Export failed - see app.log")

    def _on_reset_settings(self, _btn):
        dlg = Gtk.MessageDialog(
            transient_for=self, flags=0, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text="Reset all settings to defaults?",
        )
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.YES:
            return
        self.settings = dict(DEFAULT_SETTINGS)
        save_settings(self.settings)
        self.settings_status.set_text("Settings reset - restart the app for all fields to refresh.")
        self._apply_refresh_interval()

    # ---- GPU (beta) ----------------------------------------------------
    def _build_gpu(self):
        scroller = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(box)

        notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        notice.set_border_width(8)
        badge = Gtk.Label(label="BETA")
        badge.get_style_context().add_class("beta-badge")
        notice.pack_start(badge, False, False, 0)
        notice_txt = Gtk.Label(
            label="GPU info is experimental and read-only - detection quality depends on your "
                  "drivers and whatever's installed (lspci, vainfo, nvidia-smi, vulkaninfo).",
            xalign=0)
        notice_txt.set_line_wrap(True)
        notice_txt.get_style_context().add_class("warn-text")
        notice.pack_start(notice_txt, True, True, 0)
        box.pack_start(notice, False, False, 0)

        card = make_card("GPU INFO", badge="BETA")
        self.gpu_view = Gtk.TextView()
        self.gpu_view.set_editable(False)
        self.gpu_view.set_monospace(True)
        sw = Gtk.ScrolledWindow()
        sw.add(self.gpu_view)
        sw.set_min_content_height(360)
        card.pack_start(sw, True, True, 0)
        refresh_btn = Gtk.Button(label="REFRESH GPU INFO")
        refresh_btn.get_style_context().add_class("accent")
        refresh_btn.connect("clicked", lambda b: self._refresh_gpu())
        card.pack_start(refresh_btn, False, False, 0)
        box.pack_start(card, True, True, 0)

        self._refresh_gpu()
        return scroller

    def _refresh_gpu(self):
        lines = ["========== GPU INFO (BETA) =========="]
        gpus = gpu_list()
        if not gpus:
            lines.append("No GPU detected via lspci (or lspci isn't installed).")
        for i, g in enumerate(gpus):
            lines.append("")
            lines.append(f"GPU {i}: {g['name']}")
            lines.append(f"  Vendor: {g['vendor']}")
            lines.append(f"  Kernel driver: {g['driver']}")

        vram = gpu_vram_info()
        if vram:
            lines.append("")
            lines.append("VRAM:")
            for card_name, total_mb, used_mb in vram:
                used_str = f"{used_mb} MB used / " if used_mb is not None else ""
                lines.append(f"  {card_name}: {used_str}{total_mb} MB total")

        nvsmi = nvidia_smi_info()
        if nvsmi:
            lines.append("")
            lines.append("nvidia-smi:")
            for row in nvsmi.splitlines():
                lines.append(f"  {row.strip()}")

        codecs = gpu_codecs()
        lines.append("")
        if codecs:
            lines.append(f"Supported codec profiles (VAAPI, via vainfo) - {len(codecs)} found:")
            for c in codecs:
                lines.append(f"  {c}")
        else:
            lines.append("Codec profiles: vainfo not installed or returned nothing.")
            lines.append("  Install it from Control tab -> Dependencies, then refresh.")

        vk = vulkan_summary()
        if vk:
            lines.append("")
            lines.append("Vulkan summary:")
            for row in vk.splitlines()[:15]:
                lines.append(f"  {row.strip()}")

        buf = self.gpu_view.get_buffer()
        buf.set_text("\n".join(lines))

    # ---- scan --------------------------------------------------------
    def _build_scan(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card = make_card("SYSTEM CAPABILITY SCAN")
        self.scan_view = Gtk.TextView()
        self.scan_view.set_editable(False)
        self.scan_view.set_monospace(True)
        sw = Gtk.ScrolledWindow()
        sw.add(self.scan_view)
        sw.set_min_content_height(320)
        card.pack_start(sw, True, True, 0)
        btn = Gtk.Button(label="RUN SCAN")
        btn.get_style_context().add_class("accent")
        btn.connect("clicked", lambda b: self._run_scan())
        card.pack_start(btn, False, False, 0)
        box.pack_start(card, True, True, 0)
        self._run_scan()
        return box

    def _run_scan(self):
        lo, hi, _ = freq_bounds_mhz()
        zones = _rapl_zones()
        z = zswap_info()
        lines = [
            "========== CAPABILITY SCAN ==========",
            f"CPU: {cpu_model()}",
            f"Vendor: {cpu_vendor()}",
            f"Cores (logical): {len(core_dirs())}",
            f"Intel P-State: {'Yes' if os.path.isdir('/sys/devices/system/cpu/intel_pstate') else 'No'}",
            f"AMD P-State: {'Yes' if os.path.isdir('/sys/devices/system/cpu/amd_pstate') else 'No'}",
            f"Boost control: {'Available' if boost_state() is not None else 'Unsupported'}",
            f"Frequency range: {lo} - {hi} MHz",
            f"Governors available: {', '.join(available_governors()) or 'unknown'}",
            f"RAPL power zone: {zones[0] if zones else 'none found'}",
            "  (older Intel RAPL registers wrap/reset often - power reading now handles that)",
            f"Swap file present: {'Yes' if swapfile_present() else 'No'}",
            f"zswap module: {'loaded' if z['available'] else 'not loaded'}",
            f"cgroup version: {'v2' if os.path.exists('/sys/fs/cgroup/cgroup.controllers') else ('v1' if os.path.isdir('/sys/fs/cgroup/memory') else 'unavailable')} (used for RAM tab limits)",
            f"GPU(s) detected: {len(gpu_list())} (see GPU tab, beta)",
        ]
        buf = self.scan_view.get_buffer()
        buf.set_text("\n".join(lines))

    # ---- logging -------------------------------------------------
    def _build_log(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card = make_card("CSV LOGGING")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.log_path_entry = Gtk.Entry()
        self.log_path_entry.set_text(os.path.expanduser("~/cpu_monitor_log.csv"))
        self.log_toggle_btn = Gtk.Button(label="START LOGGING")
        self.log_toggle_btn.get_style_context().add_class("accent")
        self.log_toggle_btn.connect("clicked", self._on_toggle_logging)
        row.pack_start(self.log_path_entry, True, True, 0)
        row.pack_start(self.log_toggle_btn, False, False, 0)
        card.pack_start(row, False, False, 0)
        self.log_status = Gtk.Label(label="Not logging.", xalign=0)
        self.log_status.get_style_context().add_class("hint-text")
        card.pack_start(self.log_status, False, False, 0)
        box.pack_start(card, False, False, 0)
        return box

    def _on_toggle_logging(self, _btn):
        if not self.logging_active:
            path = self.log_path_entry.get_text().strip()
            try:
                self.log_file = open(path, "w", newline="")
                self.log_writer = csv.writer(self.log_file)
                self.log_writer.writerow(["timestamp", "power_w", "temp_c", "throttle_events", "load_1m",
                                           "governor", "ram_used_mb", "swap_used_mb"])
                self.logging_active = True
                self.log_toggle_btn.set_label("STOP LOGGING")
                self.log_status.set_text(f"Logging to {path}")
            except Exception as e:
                self.log_status.set_text(f"Error: {e}")
        else:
            self.logging_active = False
            self.log_toggle_btn.set_label("START LOGGING")
            self.log_status.set_text("Logging stopped.")
            if self.log_file:
                self.log_file.close()
                self.log_file = None

    # ---- tick ----------------------------------------------------
    def _tick(self):
        """GLib timeout callback. Wrapped so any single bad reading (a
        sensor that vanished, a drive unplugged mid-poll, etc.) is logged
        and skipped instead of crashing the whole app / killing the
        timer (returning True below keeps the timer alive either way)."""
        try:
            self._tick_inner()
        except Exception as e:
            logger.error("tick failed: %s", e)
        return True

    def _tick_inner(self):
        self._tick_count += 1

        loads = core_loads_pct()
        freqs = core_freqs_mhz()
        self._ensure_core_bars(len(loads))
        for bar, pct, f in zip(self.core_bars, loads, freqs):
            frac = pct / 100.0
            bar.set_fraction(frac)
            fstr = f"{f} MHz" if f else "N/A"
            bar.set_text(f"{pct}%  .  {fstr}")
            ctx = bar.get_style_context()
            ctx.remove_class("warn")
            ctx.remove_class("hot")
            if frac > 0.9:
                ctx.add_class("hot")
            elif frac > 0.7:
                ctx.add_class("warn")

        waiting = waiting_for_cpu()
        self.queue_value.set_text(str(waiting))
        qctx = self.queue_value.get_style_context()
        qctx.remove_class("queue-big-warn")
        qctx.remove_class("queue-big-hot")
        if waiting >= 4:
            qctx.add_class("queue-big-hot")
        elif waiting >= 1:
            qctx.add_class("queue-big-warn")

        power = cpu_power_w()
        self.stat_power.set_value(f"{power:.1f} W" if power else "N/A", "stat-value-cyan")

        temp = cpu_temp_c()
        if temp is not None:
            cls = "stat-value-red" if temp > 85 else ("stat-value-orange" if temp > 70 else "stat-value-green")
            self.stat_temp.set_value(f"{temp:.1f}\u00b0C", cls)
        else:
            self.stat_temp.set_value("N/A")

        tcount = throttle_status()
        if tcount > 0:
            self.stat_thermal.set_value(f"THROTTLED ({tcount})", "stat-value-red")
        else:
            self.stat_thermal.set_value("STABLE", "stat-value-green")

        gov = current_governor()
        self.stat_gov.set_value(gov, "stat-value-cyan")

        l1, l5, l15 = load_avg()
        self.stat_load.set_value(f"{l1} / {l5} / {l15}")

        b = boost_state()
        if b is None:
            self.stat_boost.set_value("N/A")
        else:
            self.stat_boost.set_value("ON" if b else "OFF", "stat-value-green" if b else "stat-value-orange")
            if self.boost_switch.get_active() != b:
                self.boost_switch.set_state(b)

        # ---- RAM tab live stats ----
        mi = mem_info()
        warn_pct = self.settings.get("mem_warning_pct", 90)
        used_cls = "stat-value-red" if mi['used_pct'] >= warn_pct else "stat-value-orange"
        self.ram_stat_total.set_value(f"{mi['total_mb']} MB", "stat-value-cyan")
        self.ram_stat_used.set_value(f"{mi['used_mb']} MB ({mi['used_pct']}%)", used_cls)
        self.ram_stat_avail.set_value(f"{mi['available_mb']} MB", "stat-value-green")
        self.ram_stat_swaptotal.set_value(f"{mi['swap_total_mb']} MB", "stat-value-purple")
        swap_cls = "stat-value-red" if mi['swap_used_mb'] > 0 else "stat-value-green"
        self.ram_stat_swapused.set_value(f"{mi['swap_used_mb']} MB", swap_cls)
        self.ram_stat_cached.set_value(f"{mi['cached_mb']} MB", "stat-value-cyan")
        self.ram_stat_committed.set_value(f"{mi['committed_mb']} MB", "stat-value-purple")

        pressure = safe_call(mem_pressure_pct)
        if pressure is None:
            self.ram_stat_pressure.set_value("N/A")
        else:
            p_cls = "stat-value-red" if pressure > 10 else ("stat-value-orange" if pressure > 2 else "stat-value-green")
            self.ram_stat_pressure.set_value(f"{pressure}%", p_cls)

        self._mem_history.append(mi['used_pct'])

        if mi['used_pct'] >= warn_pct:
            self.ram_warning.set_text(
                f"[!] RAM usage is at {mi['used_pct']}% (warning threshold {warn_pct}%). "
                f"Consider closing background apps, limiting a heavy process below, or "
                f"raising the swap ratio toward zswap on the RAM tab.")
            self.ram_warning.show()
        else:
            self.ram_warning.hide()

        # Top-consumers and active-limits lists are a bit heavier (spawn `ps`),
        # so refresh them every 3s instead of every tick.
        if self._tick_count % 3 == 0:
            rows = top_mem_processes(8)
            buf = self.ram_top_view.get_buffer()
            if rows:
                lines = [f"{'PID':<8}{'RAM MB':<10}{'RAM %':<8}{'COMMAND'}"]
                for pid, rss_mb, pmem, comm in rows:
                    lines.append(f"{pid:<8}{rss_mb:<10}{pmem:<8.1f}{comm}")
                buf.set_text("\n".join(lines))
            else:
                buf.set_text("Unable to read process list.")
            self._refresh_ram_active()

        if self.logging_active and self.log_writer:
            self.log_writer.writerow([
                datetime.now().isoformat(timespec="seconds"),
                f"{power:.2f}" if power else "",
                f"{temp:.1f}" if temp is not None else "",
                tcount, l1, gov, mi['used_mb'], mi['swap_used_mb'],
            ])
            self.log_file.flush()

        # Drive tab live activity (only while that tab exists/has been built)
        if hasattr(self, "_drive_refresh_activity"):
            self._drive_refresh_activity()


def main():
    screen = Gdk.Screen.get_default()
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    # Ask for the sudo/polkit password exactly once, right now. If this
    # succeeds, every governor/frequency/boost/pstate/RAM-limit/zswap
    # change for the rest of the session goes straight through with no
    # further prompts.
    ok, msg = start_helper()
    if not ok:
        dlg = Gtk.MessageDialog(
            transient_for=None,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="Running without root privileges",
        )
        dlg.format_secondary_text(
            f"{msg}\n\nMonitoring will still work, but governor/frequency/"
            f"boost/P-state/RAM-limit changes will fail until you restart "
            f"the app and enter the password."
        )
        dlg.run()
        dlg.destroy()

    win = CPUControlApp()
    win.show_all()
    win.ram_warning.hide()
    Gtk.main()


if __name__ == "__main__":
    main()
