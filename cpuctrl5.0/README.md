# CPU CONTROL v5.0

A sleek, real-time CPU + RAM + GPU monitor and control panel for Linux Mint (all editions, including LMDE).
Monitor per-core load %, frequency, temperature, power draw, and thermal state.
Adjust CPU governor, frequency limits, turbo boost, and Intel/AMD P-State % —
now with a merged-in RAM limiter and a beta GPU info tab — all from one
dark-themed GTK app.

## What's new in v5.0

- **Drive tab** - per-drive model/vendor/type/interface/capacity/filesystem,
  used/free space, temperature, and SMART health (via `smartctl`, through
  the root helper). Live read/write speed and active-time % per drive. A
  benchmark tool (sequential/random read/write, IOPS, latency, Excellent/
  Good/Average/Slow rating) that only ever touches a private temp file it
  creates and deletes itself - your data is never at risk.
- **Settings tab** - theme, UI scaling, start minimized/with system, refresh
  rate (fast/balanced/low-resource), background monitoring toggle,
  notifications, history length, memory warning threshold, low-resource
  mode, benchmark defaults, debug logging, export diagnostics, reset all
  settings. Saved to `~/.config/cpu-control/settings.json` and applied
  immediately.
- **RAM tab additions** - cached, committed, and memory-pressure (PSI)
  readings, plus a low-memory warning banner when usage crosses your
  threshold.
- **Stability** - every periodic refresh is now wrapped so one bad sensor
  read can't crash the app; errors go to a rotating log
  (`~/.config/cpu-control/app.log`) instead of stderr; in-memory history is
  now a bounded ring buffer instead of growing forever.

## System Requirements

**Linux Mint, all editions** (Cinnamon, MATE, Xfce) **and LMDE** — works on
any Debian/Ubuntu-based distro with the right deps

- **Python 3.11+** (installer will offer to install this for you if missing)
- **GTK 3.0** (python3-gi, gir1.2-gtk-3.0)
- **PolicyKit 1** (policykit-1) — for secure sudo prompts
- **jq** — used by the RAM tab to track active limits
- **pciutils** (`lspci`) — used by the GPU tab
- **lm-sensors** (optional, for better temp/power reading, especially on AMD or older Intel)
- **cpupower** (optional, for deeper CPU info)
- **vainfo** / **nvidia-smi** / **vulkaninfo** (all optional, feed the GPU tab's codec list)

The installer checks for all of the above and installs what it can
automatically (needs `sudo`).

## Installation

1. **Download all files** into a single folder:
   - `cpu_ctrl_v5_0.py`
   - `cpu_ctrl_helper`
   - `ramlimit2_0.sh` (kept as a standalone CLI fallback; the GUI doesn't need it)
   - `cpu-control.desktop`
   - `install.sh`
   - `requirements.txt`

2. **Run the installer**:
   ```bash
   chmod +x install.sh
   sudo ./install.sh
   ```
   This first checks your Python version. If it's below 3.11, it'll ask you
   which version to install (3.11 / 3.12 / 3.13) and pull it in via the
   deadsnakes PPA on Ubuntu-base Mint editions, or directly via `apt` on
   LMDE (Debian base, no deadsnakes). It then checks/installs system packages (GTK3,
   PolicyKit, jq, pciutils) via `apt`, installs Python dependencies from
   `requirements.txt`, verifies everything is actually importable, then
   copies everything into `/opt/cpu-control` and registers the app in your
   menu. If a required dependency is still missing after install, the
   script aborts with a clear error instead of installing a broken app.

3. **Done**. Look for "CPU Control" in your XFCE application menu (System category), or launch directly:
   ```bash
   python3 /opt/cpu-control/cpu_ctrl_v5_0.py
   ```

## First Launch

When you open CPU Control for the **first time**, you'll see a **single sudo/PolicyKit password prompt**. This is safe and normal—it authenticates you as root *once* for the entire session.

- **If you enter your password**: every governor/frequency/boost/P-State
  change, and every RAM limit / zswap / swap-ratio change, works instantly
  afterward, with **no more prompts**.
- **If you cancel the prompt**: the app still runs in read-only mode
  (monitoring works fine, but control changes will fail).

## How to Use

### Dashboard Tab
- **Live Status**: Power draw, temperature, thermal state, current governor, system load, boost status
- **Per-Core Load**: Green (0–70%) / Orange (70–90%) / Red (90–100%) bars + MHz per core
- **Waiting for CPU**: 0 = smooth sailing, 1–3 = some contention, 4+ = bottleneck

### Control Tab
- **Scaling Governor**, **Max Frequency Limit**, **Turbo Boost**
- **Intel/AMD P-State Max Performance %** — label adapts to your CPU vendor
- **Install/Check Dependencies** — pulls in lm-sensors, cpupower, jq, vainfo

### RAM Tab
- **Memory Overview** — total/used/available RAM, swap total/used
- **Top Memory Consumers** — refreshes every few seconds
- **Limit a Process** — enter a PID + limit (MB), choose Push to Swap (soft
  cap, process keeps running) or Hard Limit (no swap, OOM-killed if it goes
  over), Apply
- **Active Limits** — live table of everything currently capped, with
  per-PID or clear-all buttons
- **zswap** — enable/disable, pick a compressor, set the RAM pool %
- **Swap Strategy** — disk-vs-zswap ratio presets (or a custom slider);
  each apply asks for confirmation since it briefly takes swap offline to
  resize it

### GPU Tab (BETA)
Model, vendor, kernel driver, VRAM, and — where `vainfo`/`nvidia-smi` are
installed — supported codec profiles. Read-only, no controls. Quality of
the results depends on what's installed on your system; use "Install/Check
Dependencies" on the Control tab to add `vainfo`, then refresh.

### Scan Tab
System capability scan: CPU model/vendor, core count, P-State support,
frequency range, governors, RAPL power zone, cgroup version, swap/zswap
state, and GPU count.

### Logging Tab
Logs live stats (power, temp, throttle events, load, governor, RAM/swap
used) to a CSV file for later analysis.

## Tips & Tricks

- **Power saving**: `powersave` governor + lower max frequency
- **Gaming/workload**: `performance` governor + turbo boost enabled
- **Thermal throttling**: red "THROTTLED" count means check cooling or lower max frequency
- **RAM-hungry background app**: Push-to-Swap limit keeps it running but capped; Hard Limit kills it outright if it overshoots
- **Low on RAM but plenty of disk**: favor disk in the swap-ratio (70/30 or higher)
- **Fast NVMe / limited RAM**: favor zswap (30/70 or 10/90) for less disk I/O

## Troubleshooting

**"Not authorized as root yet" error on a control or RAM change?**
- You cancelled the initial password prompt. Restart the app and enter your password when prompted.

**App won't launch at all?**
- Confirm Python is 3.11+: `python3 --version` (or whichever interpreter the installer picked — check `/usr/share/applications/cpu-control.desktop`'s `Exec=` line)
- Confirm GTK 3 is importable under that interpreter: `python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; print('OK')"`

**Can't see CPU temperatures / power draw looks off (especially older Intel or AMD)?**
- Run `sudo sensors-detect` once to set up lm-sensors properly
- Older Intel chips: power now corrects for RAPL counter wraparound automatically — if it still reads N/A, RAPL likely isn't exposed on that board at all and there's no fallback except `sensors`
- AMD: make sure `k10temp` (temps) is loaded; RAPL-style power needs a kernel with `amd-rapl`/`amd_energy` powercap support

**RAM limit won't apply / active limits show nothing?**
- Needs `jq` and a cgroup v1 or v2 hierarchy — check the Scan tab's "cgroup version" line
- Systemd may fight a reparented PID under certain scopes; if a limit doesn't stick, that's almost always why

**GPU tab is empty or thin?**
- It's BETA and read-only. Install `vainfo` via Control tab → Dependencies for the codec list; `nvidia-smi` needs proprietary NVIDIA drivers already installed

**Can't change frequency or governor?**
- Some CPUs/systems use different control interfaces — check the Scan tab
- Set governor first, then frequency, if changes aren't sticking

## Uninstall

```bash
sudo rm -rf /opt/cpu-control
sudo rm /usr/share/applications/cpu-control.desktop
sudo update-desktop-database /usr/share/applications
```
Note: this doesn't clear any RAM limits or cgroups you have active — use
the RAM tab's "Clear All Limits" (or `ram_clear_all` via the helper) before
uninstalling if you want those cleaned up too.

## License & Attribution

Built for Linux Mint (all editions, incl. LMDE). Reads directly from `/sys/devices/system/cpu`,
`/proc`, `/sys/fs/cgroup`, and `/sys/class/drm` — no daemons needed beyond
the one-time-authenticated helper process (just GTK for the UI).

---

**Questions?** Check the Scan tab to see what your system supports, then adjust accordingly.
