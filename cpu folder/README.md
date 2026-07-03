# CPU CONTROL v3.5

A sleek, real-time CPU monitor and control panel for Linux Mint / XFCE. Monitor per-core load %, frequency, temperature, power draw, and thermal state. Adjust CPU governor, frequency limits, turbo boost, and Intel P-State % — all from one dark-themed GTK app.

## System Requirements

**Linux Mint 19.x through 21.x** (works on any XFCE-based distro with the right deps)

- **Python 3.6+**
- **GTK 3.0** (python3-gi, gir1.2-gtk-3.0)
- **PolicyKit 1** (policykit-1) — for secure sudo prompts
- **lm-sensors** (optional, for better temp reading)
- **cpupower** (optional, for deeper CPU info)

The installer checks for GTK and PolicyKit; if missing, it installs them automatically (needs `sudo`).

## Installation

1. **Download all 4 files** into a single folder:
   - `cpu_ctrl_v3_5.py`
   - `cpu_ctrl_helper`
   - `cpu-control.desktop`
   - `install.sh`

2. **Run the installer**:
   ```bash
   chmod +x install.sh
   sudo ./install.sh
   ```
   This copies everything into `/opt/cpu-control` and registers the app in your menu.

3. **Done**. Look for "CPU Control" in your XFCE application menu (System category), or launch directly:
   ```bash
   python3 /opt/cpu-control/cpu_ctrl_v3_5.py
   ```

## First Launch

When you open CPU Control for the **first time**, you'll see a **single sudo/PolicyKit password prompt**. This is safe and normal—it authenticates you as root *once* for the entire session.

- **If you enter your password**: every governor/frequency/boost/P-State change you make after that works instantly, with **no more prompts**.
- **If you cancel the prompt**: the app still runs in read-only mode (monitoring works fine, but control changes will fail).

**Why?** The old way asked for a password every single time you tweaked something. Now you authorize once, and the rest of the session is frictionless.

## How to Use

### Dashboard Tab
- **Live Status**: Power draw, temperature, thermal state, current governor, system load, boost status
- **Per-Core Load**: Shows each CPU core as a green→orange→red bar (0–100%), plus its current frequency in MHz
  - **Green** (0–70%) = healthy
  - **Orange** (70–90%) = warm
  - **Red** (90–100%) = hot
- **Waiting for CPU**: How many processes/apps are ready to run but stuck waiting for a free core to open up
  - **0** = smooth sailing
  - **1–3** = some contention (orange)
  - **4+** = bottleneck (red)

### Control Tab
- **Scaling Governor**: Switch between available CPU governors (powersave, performance, ondemand, etc.)
- **Max Frequency Limit**: Cap your CPU's max clock speed (e.g., `3.5GHz`, `3200MHz`, or `default` for hardware max)
- **Turbo Boost**: Toggle Intel Turbo Boost or AMD Turbo (if available)
- **Intel P-State Max Performance %**: Slider to limit max CPU performance on Intel systems (1–100%)
- **Install/Check Dependencies**: Downloads lm-sensors and cpupower if missing

### Scan Tab
Shows your system's CPU capability: model name, core count, available governors, frequency range, boost support, RAPL power interface, etc.

### Logging Tab
Logs live stats (power, temp, throttle events, load, governor) to a CSV file for later analysis.

## Tips & Tricks

- **Power saving**: Set governor to `powersave` and lower max frequency to reduce heat and power draw
- **Gaming/workload**: Switch to `performance` governor + enable turbo boost for max speed
- **Thermal throttling**: If you see "THROTTLED" with a red count, your CPU is overheating—check cooling, clean vents, or lower max frequency
- **Per-core load bars**: Use these to spot uneven load across cores (can indicate a single-threaded bottleneck or bad thread affinity)

## Troubleshooting

**"Not authorized as root yet" error on a control change?**
- You cancelled the initial password prompt. Restart the app and enter your password when prompted.

**App won't launch at all?**
- Make sure all 4 files are together in the same folder (if testing without install)
- If installed, check that `/opt/cpu-control/cpu_ctrl_v3_5.py` exists and has the right permissions
- Confirm GTK 3 is installed: `python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; print('OK')"`

**Can't see CPU temperatures?**
- Run `sudo sensors-detect` once to set up lm-sensors properly
- Some laptops hide temp sensors; you may see "N/A"

**Can't change frequency or governor?**
- Some CPUs/systems use different control interfaces. The Scan tab will show what's available
- Check that cpupower is installed: `dpkg -l | grep cpupower` (or run the Dependencies installer from the Control tab)

**Frequency changes don't stick?**
- Verify the governor is set to something that allows manual frequency control (not `schedutil` or `powersave` on some systems)
- Try setting governor first, *then* frequency

## Uninstall

```bash
sudo rm -rf /opt/cpu-control
sudo rm /usr/share/applications/cpu-control.desktop
sudo update-desktop-database /usr/share/applications
```

## License & Attribution

Built for Linux Mint / XFCE. Reads directly from `/sys/devices/system/cpu` and `/proc` — no external libraries or daemons needed (just GTK for the UI).

---

**Questions?** Check the Scan tab to see what your system supports, then adjust accordingly.
