#!/usr/bin/env python3
"""
CPU CONTROL v3.5 - real GTK desktop app for Linux Mint / XFCE
Reads live system data (frequencies, load, power, thermal) and lets you
change governor / max freq / boost / Intel P-State % through a small
root-only helper invoked via pkexec (so this app never needs to run as root).
"""

import os
import re
import glob
import time
import subprocess
import csv
from datetime import datetime

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(APP_DIR, "cpu_ctrl_helper")

CSS = b"""
window { background-color: #000000; }
* { font-family: 'Share Tech Mono', 'Ubuntu Mono', monospace; color: #9aa5b1; }

.logo {
    font-family: 'Orbitron', sans-serif;
    font-weight: 900;
    font-size: 15px;
    letter-spacing: 3px;
    color: #e2eaf2;
}
.logo-fx { color: #00ff88; }
.vbadge { color: #ff8c00; font-size: 10px; letter-spacing: 1px; }

.card {
    background-color: #060606;
    border: 1px solid #161616;
    border-radius: 8px;
}
.ctit {
    font-family: 'Orbitron', sans-serif;
    font-weight: 700;
    font-size: 10px;
    letter-spacing: 2px;
    color: #3a3a3a;
}
.stat-label { font-size: 10px; color: #3a3a3a; letter-spacing: 1px; }
.stat-value { font-size: 16px; color: #e2eaf2; font-weight: bold; }
.stat-value-green { color: #00ff88; }
.stat-value-cyan { color: #00d4ff; }
.stat-value-orange { color: #ff8c00; }
.stat-value-red { color: #ff2d55; }

.core-label { font-size: 10px; color: #8a8f96; }
progressbar trough { background-color: #0a0a0a; border-radius: 4px; min-height: 10px; border: 1px solid #161616; }
progressbar progress { background-color: #00ff88; border-radius: 4px; min-height: 10px; }
progressbar.warn progress { background-color: #ff8c00; }
progressbar.hot progress { background-color: #ff2d55; }

button {
    background-color: #0a0a0a;
    border: 1px solid #1e1e1e;
    border-radius: 6px;
    color: #9aa5b1;
    padding: 6px 12px;
}
button:hover { background-color: #141414; border-color: #00d4ff; color: #00d4ff; }
button.accent { border-color: #00ff88; color: #00ff88; }
button.danger { border-color: #ff2d55; color: #ff2d55; }

combobox button, entry {
    background-color: #000000;
    border: 1px solid #1e1e1e;
    border-radius: 4px;
    color: #e2eaf2;
    padding: 4px 8px;
}

notebook header {
    background-color: #060606;
    border-bottom: 2px solid #161616;
}
notebook tab {
    padding: 8px 14px;
}
notebook tab label {
    font-family: 'Orbitron', sans-serif;
    font-size: 9px;
    letter-spacing: 1.5px;
    color: #3a3a3a;
}
notebook tab:checked label { color: #00d4ff; }

textview, textview text {
    background-color: #000000;
    color: #00ff88;
    font-family: 'Share Tech Mono', monospace;
}

.queue-big { font-size: 34px; color: #e2eaf2; font-weight: bold; }
.queue-big-warn { color: #ff8c00; }
.queue-big-hot { color: #ff2d55; }
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


_LAST_ENERGY = {"e": None, "t": None}


def cpu_power_w():
    path = "/sys/class/powercap/intel-rapl:0/energy_uj"
    if os.path.exists(path):
        try:
            e = int(read_file(path))
        except (TypeError, ValueError):
            return None
        t = time.time()
        if _LAST_ENERGY["e"] is not None and e >= _LAST_ENERGY["e"]:
            de = e - _LAST_ENERGY["e"]
            dt = t - _LAST_ENERGY["t"]
            _LAST_ENERGY["e"], _LAST_ENERGY["t"] = e, t
            if dt > 0:
                return (de / 1_000_000) / dt
        _LAST_ENERGY["e"], _LAST_ENERGY["t"] = e, t
        return None
    try:
        out = subprocess.run(["sensors"], capture_output=True, text=True, timeout=2).stdout
        m = re.search(r"(?:PPT|Package|power1)\D+([\d.]+)\s*W", out, re.I)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


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
    password; every control action afterwards is sent to this same process
    over a pipe, so no further prompts appear.
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


def run_helper(action, value=""):
    """Send one action to the already-authenticated persistent helper.
    Returns (ok, message). No password prompt happens here."""
    global _HELPER_PROC
    if _HELPER_PROC is None or _HELPER_PROC.poll() is not None:
        return False, "Not authorized as root yet - restart the app"
    with _get_lock():
        try:
            line = f"{action} {value}".strip() + "\n"
            _HELPER_PROC.stdin.write(line)
            _HELPER_PROC.stdin.flush()
            out = _HELPER_PROC.stdout.readline().strip()
        except Exception as e:
            return False, f"Lost connection to privileged helper: {e}"
        if out.startswith("OK"):
            return True, "OK"
        return False, out or "Unknown error"


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
        for c in ("stat-value-green", "stat-value-cyan", "stat-value-orange", "stat-value-red"):
            ctx.remove_class(c)
        if cls:
            ctx.add_class(cls)


def make_card(title):
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    card.get_style_context().add_class("card")
    card.set_margin_top(8)
    card.set_margin_bottom(8)
    card.set_margin_start(8)
    card.set_margin_end(8)
    card.set_property("margin", 10)
    lbl = Gtk.Label(label=title, xalign=0)
    lbl.get_style_context().add_class("ctit")
    card.pack_start(lbl, False, False, 0)
    return card


class CPUControlApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="CPU CONTROL v3.5")
        self.set_default_size(760, 620)
        self.set_border_width(0)
        self.connect("destroy", self._on_destroy)

        self.logging_active = False
        self.log_writer = None
        self.log_file = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        outer.pack_start(self._build_header(), False, False, 0)

        self.notebook = Gtk.Notebook()
        outer.pack_start(self.notebook, True, True, 0)

        self.notebook.append_page(self._build_dashboard(), Gtk.Label(label="DASHBOARD"))
        self.notebook.append_page(self._build_control(), Gtk.Label(label="CONTROL"))
        self.notebook.append_page(self._build_scan(), Gtk.Label(label="SCAN"))
        self.notebook.append_page(self._build_log(), Gtk.Label(label="LOGGING"))

        GLib.timeout_add(1000, self._tick)
        self._tick()

    def _on_destroy(self, _win):
        stop_helper()
        Gtk.main_quit()

    # ---- header --------------------------------------------------
    def _build_header(self):
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hdr.set_border_width(10)
        hdr.get_style_context().add_class("card")

        logo = Gtk.Label()
        logo.set_markup('<span foreground="#e2eaf2">CPU</span><span foreground="#00ff88">CTRL</span>')
        logo.get_style_context().add_class("logo")
        hdr.pack_start(logo, False, False, 0)

        badge = Gtk.Label(label="v3.5")
        badge.get_style_context().add_class("vbadge")
        hdr.pack_start(badge, False, False, 0)

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
        self.queue_sub.get_style_context().add_class("stat-label")
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
        boost_row.pack_start(Gtk.Label(label="Enable boost / turbo"), False, False, 0)
        boost_row.pack_start(self.boost_switch, False, False, 0)
        boost_card.pack_start(boost_row, False, False, 0)
        box.pack_start(boost_card, False, False, 0)

        pstate_card = make_card("INTEL P-STATE MAX PERFORMANCE %")
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
        box.pack_start(self.control_status, False, False, 6)

        deps_card = make_card("DEPENDENCIES")
        deps_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        deps_btn = Gtk.Button(label="INSTALL / CHECK (lm-sensors, cpupower)")
        deps_btn.connect("clicked", self._on_install_deps)
        deps_row.pack_start(deps_btn, False, False, 0)
        deps_card.pack_start(deps_row, False, False, 0)
        box.pack_start(deps_card, False, False, 0)

        return scroller

    def _set_status(self, ok, msg):
        self.control_status.set_text(("✔ " if ok else "✘ ") + msg)

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

    # ---- scan --------------------------------------------------------
    def _build_scan(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card = make_card("SYSTEM CAPABILITY SCAN")
        self.scan_view = Gtk.TextView()
        self.scan_view.set_editable(False)
        self.scan_view.set_monospace(True)
        sw = Gtk.ScrolledWindow()
        sw.add(self.scan_view)
        sw.set_min_content_height(300)
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
            f"RAPL power interface: {'Yes' if os.path.exists('/sys/class/powercap/intel-rapl:0/energy_uj') else 'No'}",
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
        card.pack_start(self.log_status, False, False, 0)
        box.pack_start(card, False, False, 0)
        return box

    def _on_toggle_logging(self, _btn):
        if not self.logging_active:
            path = self.log_path_entry.get_text().strip()
            try:
                self.log_file = open(path, "w", newline="")
                self.log_writer = csv.writer(self.log_file)
                self.log_writer.writerow(["timestamp", "power_w", "temp_c", "throttle_events", "load_1m", "governor"])
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
        loads = core_loads_pct()
        freqs = core_freqs_mhz()
        self._ensure_core_bars(len(loads))
        for bar, pct, f in zip(self.core_bars, loads, freqs):
            frac = pct / 100.0
            bar.set_fraction(frac)
            fstr = f"{f} MHz" if f else "N/A"
            bar.set_text(f"{pct}%  ·  {fstr}")
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
            self.stat_temp.set_value(f"{temp:.1f}°C", cls)
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

        if self.logging_active and self.log_writer:
            self.log_writer.writerow([
                datetime.now().isoformat(timespec="seconds"),
                f"{power:.2f}" if power else "",
                f"{temp:.1f}" if temp is not None else "",
                tcount, l1, gov,
            ])
            self.log_file.flush()

        return True


def main():
    screen = Gdk.Screen.get_default()
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    # Ask for the sudo/polkit password exactly once, right now. If this
    # succeeds, every governor/frequency/boost/pstate change for the rest
    # of the session goes straight through with no further prompts.
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
            f"boost/P-state changes will fail until you restart the app "
            f"and enter the password."
        )
        dlg.run()
        dlg.destroy()

    win = CPUControlApp()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
