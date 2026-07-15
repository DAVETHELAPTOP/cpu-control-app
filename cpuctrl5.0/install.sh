#!/usr/bin/env bash
# Installer for CPU Control v5.0 - Linux Mint (all editions, incl. LMDE)
set -euo pipefail

INSTALL_DIR="/opt/cpu-control"
DESKTOP_DIR="/usr/share/applications"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASELINE_MAJOR=3
BASELINE_MINOR=11

echo "== CPU Control v5.0 installer =="

# --- Basic tool checks (run before any sudo re-exec so failures are clear) ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: no python3 found at all. Install Python ${BASELINE_MAJOR}.${BASELINE_MINOR}+ and re-run this script."
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "This step needs sudo to install into $INSTALL_DIR, install dependencies,"
    echo "and register the app menu entry."
    exec sudo bash "$0" "$@"
fi

# --- Baseline: Python 3.11+ required. Find the newest interpreter already
# on the system that meets it; if none do, offer to install one. ---
version_ge() {
    # $1 >= $2 ? for "major.minor" strings
    local a_maj=${1%%.*} a_min=${1#*.}
    local b_maj=${2%%.*} b_min=${2#*.}
    if (( a_maj > b_maj )); then return 0; fi
    if (( a_maj == b_maj && a_min >= b_min )); then return 0; fi
    return 1
}

find_best_python() {
    local best="" best_ver="0.0"
    for cand in python3.14 python3.13 python3.12 python3.11 python3; do
        command -v "$cand" >/dev/null 2>&1 || continue
        local v
        v=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
        if version_ge "$v" "$best_ver"; then
            best="$cand"; best_ver="$v"
        fi
    done
    echo "${best}:${best_ver}"
}

IFS=':' read -r PY_BIN PY_VER <<< "$(find_best_python)"

if [[ -z "$PY_BIN" ]] || ! version_ge "$PY_VER" "${BASELINE_MAJOR}.${BASELINE_MINOR}"; then
    echo ""
    echo "CPU Control v5.0 needs Python ${BASELINE_MAJOR}.${BASELINE_MINOR} or newer."
    if [[ -n "$PY_BIN" ]]; then
        echo "Newest Python found on this system is ${PY_BIN} (${PY_VER}), which is too old."
    else
        echo "No usable python3 was found."
    fi
    echo ""
    echo "Which version should I install?"
    echo "  1) Python 3.11"
    echo "  2) Python 3.12"
    echo "  3) Python 3.13"
    read -rp "Select [1-3, default 2]: " pychoice
    case "${pychoice:-2}" in
        1) TARGET_VER="3.11" ;;
        3) TARGET_VER="3.13" ;;
        *) TARGET_VER="3.12" ;;
    esac

    echo "Installing Python ${TARGET_VER}..."
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "ERROR: no apt-get on this system - install Python ${TARGET_VER} manually, then re-run."
        exit 1
    fi

    # Ubuntu-base Mint editions (Cinnamon/MATE/Xfce) can use deadsnakes.
    # LMDE (Debian base) has no deadsnakes PPA - apt directly instead,
    # since Debian 12+ ships modern Python in its own repos.
    IS_UBUNTU_BASE=0
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        if [[ "${ID_LIKE:-}" == *ubuntu* || "${ID:-}" == "ubuntu" ]]; then
            IS_UBUNTU_BASE=1
        fi
    fi

    apt-get update -y
    if [[ $IS_UBUNTU_BASE -eq 1 ]]; then
        apt-get install -y software-properties-common
        add-apt-repository -y ppa:deadsnakes/ppa
        apt-get update -y
    fi
    apt-get install -y "python${TARGET_VER}" "python${TARGET_VER}-venv" "python${TARGET_VER}-dev" \
        || { echo "ERROR: apt could not install python${TARGET_VER} on this base - install it manually, then re-run."; exit 1; }

    PY_BIN="python${TARGET_VER}"
    if ! command -v "$PY_BIN" >/dev/null 2>&1; then
        echo "ERROR: install of $PY_BIN appears to have failed."
        exit 1
    fi
    PY_VER=$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    echo "Now using $PY_BIN ($PY_VER)."
else
    echo "Using existing $PY_BIN (Python $PY_VER) - meets the ${BASELINE_MAJOR}.${BASELINE_MINOR}+ baseline."
fi

# --- System packages (apt) ---
# PyGObject needs the actual GTK3 introspection libs; these only come from
# apt, not pip, so we always make sure they're present first. jq/lspci/
# vainfo back the new RAM and GPU tabs.
NEED_APT=0
"$PY_BIN" -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null || NEED_APT=1
command -v pkexec >/dev/null 2>&1 || NEED_APT=1
command -v pip3 >/dev/null 2>&1 || NEED_APT=1
command -v jq >/dev/null 2>&1 || NEED_APT=1
command -v lspci >/dev/null 2>&1 || NEED_APT=1
command -v lsblk >/dev/null 2>&1 || NEED_APT=1
command -v smartctl >/dev/null 2>&1 || NEED_APT=1

if [[ $NEED_APT -eq 1 ]]; then
    echo "Installing system dependencies (python3-gi, GTK3, PolicyKit, pip, jq, pciutils, util-linux, smartmontools)..."
    apt-get update -y
    apt-get install -y python3-gi gir1.2-gtk-3.0 policykit-1 python3-pip jq pciutils util-linux smartmontools
fi

# If we're on a non-default (deadsnakes) interpreter, python3-gi from apt is
# built against the *system* python3, not this one. Try a matching gi
# package first; if that's not packaged, fall back to pip-installing
# PyGObject straight into this interpreter (the GTK3 dev headers/libs from
# the apt step above are what it needs to build against).
if ! "$PY_BIN" -c "import gi" 2>/dev/null; then
    PY_SHORT="${PY_BIN#python}"
    apt-get install -y "python${PY_SHORT}-gi" 2>/dev/null || true
    if ! "$PY_BIN" -c "import gi" 2>/dev/null; then
        apt-get install -y libgirepository1.0-dev libcairo2-dev pkg-config >/dev/null 2>&1 || true
        "$PY_BIN" -m pip install --break-system-packages PyGObject 2>/dev/null \
            || "$PY_BIN" -m pip install PyGObject 2>/dev/null || true
    fi
fi

# --- Python packages (pip, via requirements.txt) ---
if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
    echo "Checking Python package requirements..."
    "$PY_BIN" -m pip install --break-system-packages -r "$SCRIPT_DIR/requirements.txt" \
        || "$PY_BIN" -m pip install -r "$SCRIPT_DIR/requirements.txt" \
        || echo "WARNING: pip install had issues; will still verify below."
else
    echo "WARNING: requirements.txt not found next to install.sh, skipping pip step."
fi

# --- Final verification: make sure everything actually works ---
echo "Verifying dependencies..."
MISSING=0

if ! "$PY_BIN" -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
    echo "  [MISSING] GTK3 / PyGObject (gi) is still not importable under $PY_BIN."
    MISSING=1
else
    echo "  [OK] GTK3 / PyGObject (via $PY_BIN)"
fi

if ! command -v pkexec >/dev/null 2>&1; then
    echo "  [MISSING] pkexec (PolicyKit) not found."
    MISSING=1
else
    echo "  [OK] PolicyKit (pkexec)"
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "  [MISSING] jq not found (needed for the RAM tab's limit tracking)."
    MISSING=1
else
    echo "  [OK] jq"
fi

if command -v sensors >/dev/null 2>&1; then
    echo "  [OK] lm-sensors (optional)"
else
    echo "  [INFO] lm-sensors not found (optional, better temp/power readings on older or AMD boards)"
fi

if command -v cpupower >/dev/null 2>&1; then
    echo "  [OK] cpupower (optional)"
else
    echo "  [INFO] cpupower not found (optional, deeper CPU info)"
fi

if command -v vainfo >/dev/null 2>&1; then
    echo "  [OK] vainfo (optional, powers the GPU tab's codec list)"
else
    echo "  [INFO] vainfo not found (optional - GPU tab codec list will be empty until installed)"
fi

if command -v smartctl >/dev/null 2>&1; then
    echo "  [OK] smartmontools (powers Drive tab SMART health)"
else
    echo "  [INFO] smartctl not found (optional - Drive tab health will show N/A until installed)"
fi

if command -v lsblk >/dev/null 2>&1; then
    echo "  [OK] lsblk (powers the Drive tab)"
else
    echo "  [MISSING] lsblk not found - Drive tab won't be able to list drives."
    MISSING=1
fi

if [[ $MISSING -eq 1 ]]; then
    echo ""
    echo "ERROR: one or more required dependencies are missing. Aborting install."
    echo "Try running: sudo apt-get install python3-gi gir1.2-gtk-3.0 policykit-1 jq"
    exit 1
fi

mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/cpu_ctrl_v5_0.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/cpu_ctrl_helper" "$INSTALL_DIR/"
[[ -f "$SCRIPT_DIR/ramlimit2_0.sh" ]] && cp "$SCRIPT_DIR/ramlimit2_0.sh" "$INSTALL_DIR/"
[[ -f "$SCRIPT_DIR/requirements.txt" ]] && cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR/cpu_ctrl_v5_0.py"
chmod 755 "$INSTALL_DIR/cpu_ctrl_helper"
[[ -f "$INSTALL_DIR/ramlimit2_0.sh" ]] && chmod 755 "$INSTALL_DIR/ramlimit2_0.sh"

sed "s|Exec=.*|Exec=${PY_BIN} $INSTALL_DIR/cpu_ctrl_v5_0.py|" \
    "$SCRIPT_DIR/cpu-control.desktop" > "$DESKTOP_DIR/cpu-control.desktop"
chmod 644 "$DESKTOP_DIR/cpu-control.desktop"

update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo ""
echo "Done. Find 'CPU Control' in your XFCE application menu (System category),"
echo "or launch it directly with:  ${PY_BIN} $INSTALL_DIR/cpu_ctrl_v5_0.py"
echo ""
echo "New in v5.0: Drive tab (info, activity, benchmark), full Settings tab"
echo "(persisted to ~/.config/cpu-control/settings.json), memory pressure /"
echo "cached / committed RAM stats, and crash-hardened background monitoring."
