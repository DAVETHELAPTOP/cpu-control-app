#!/bin/bash

################################################################################
# RAM LIMITER 2.0 - Advanced Memory & Swap Management Tool
# Features: Process RAM limiting, zswap/diskswap control, intelligent filtering
#
# CHANGES FROM 1.0:
#  - Fixed zswap max_pool_percent bug (was writing bytes into a percent field)
#  - Disk swap ratio now actually resizes the swapfile instead of printing
#    numbers and doing nothing
#  - Compressor selection validated against what the kernel actually supports
#  - Warns you if a target PID is owned by a systemd scope/service before
#    trying to yank it into a custom cgroup (systemd may fight back / revert it)
################################################################################

set -euo pipefail

# Colors & Styling
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
WHITE='\033[1;37m'
BOLD='\033[1m'
NC='\033[0m'

# Config file
CONFIG_DIR="${HOME}/.config/ramlimit"
CONFIG_FILE="${CONFIG_DIR}/config.json"
LIMITS_FILE="${CONFIG_DIR}/limits.json"

# Initialize config directory
init_config() {
    mkdir -p "$CONFIG_DIR"
    
    if [[ ! -f "$CONFIG_FILE" ]]; then
        cat > "$CONFIG_FILE" << 'EOF'
{
  "swap_strategy": "zswap",
  "disk_swap_ratio": 50,
  "zswap_ratio": 50,
  "zswap_enabled": true,
  "monitor_interval": 2,
  "exclude_processes": ["kernel", "systemd", "sshd", "cron", "dbus", "udev"],
  "global_ram_limit_percent": 80
}
EOF
    fi
    
    if [[ ! -f "$LIMITS_FILE" ]]; then
        echo '{}' > "$LIMITS_FILE"
    fi
}

# Check for root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}✗ This tool requires root privileges${NC}"
        echo "Run with: sudo $0"
        exit 1
    fi
}

# Get system info
get_system_info() {
    local total_ram=$(free -h | grep Mem | awk '{print $2}')
    local used_ram=$(free -h | grep Mem | awk '{print $3}')
    local available_ram=$(free -h | grep Mem | awk '{print $7}')
    local swap_total=$(free -h | grep Swap | awk '{print $2}')
    local swap_used=$(free -h | grep Swap | awk '{print $3}')
    local ram_percent=$(($(free | grep Mem | awk '{print $3}') * 100 / $(free | grep Mem | awk '{print $2}')))
    
    cat << EOF
    Total RAM: $total_ram | Used: $used_ram | Available: $available_ram
    Swap Total: $swap_total | Used: $swap_used
    RAM Usage: ${ram_percent}%
EOF
}

# Filter processes (exclude system noise)
filter_processes() {
    local exclude_pattern="(kernel|systemd|sshd|kthreadd|cron|dbus|udev|NetworkManager|docker|containerd|systemd-|kworker|kswapd|ksoftirqd|migration|watchdog|irq|rcuos|rcuop)"
    
    ps aux --sort=-%mem | awk 'NR>1 {
        if ($1 !~ /root|_/ && $11 !~ /\[/ && $11 !~ /^(kernel|systemd|sshd|kthreadd|cron|dbus|udev|NetworkManager|docker)/) {
            print $2, $4, $11
        }
    }' | sort -k2 -rn
}

# Get top memory consuming processes
show_top_processes() {
    local limit=${1:-10}
    echo -e "\n${CYAN}=== TOP MEMORY CONSUMERS ===${NC}"
    echo -e "${BOLD}RANK  PID         RAM%   COMMAND${NC}"
    echo "──────────────────────────────────────"
    
    local rank=1
    filter_processes | head -n "$limit" | while read pid ram_percent cmd; do
        local ram_mb=$(($(ps -p "$pid" -o rss= 2>/dev/null || echo 0) / 1024))
        printf "${GREEN}%-4d${NC}  %-10s %-6.2f%% %s\n" "$rank" "$pid" "$ram_percent" "$cmd"
        ((rank++))
    done
}

# Detect cgroup version
detect_cgroup_version() {
    if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
        echo "v2"
    elif [[ -d /sys/fs/cgroup/memory ]]; then
        echo "v1"
    else
        echo "unknown"
    fi
}

# Check if a PID currently lives under a systemd-managed cgroup
# (user session scope, .service, .scope, etc). If so, systemd may move it
# back or refuse the reparent on its next cgroup reconciliation pass.
check_systemd_owned() {
    local pid=$1
    local cg_path
    cg_path=$(awk -F: '$1==0{print $3}' "/proc/${pid}/cgroup" 2>/dev/null)

    if [[ -z "$cg_path" ]]; then
        return 1
    fi

    if [[ "$cg_path" == *.scope* || "$cg_path" == *.service* || "$cg_path" == *user.slice* || "$cg_path" == *system.slice* ]]; then
        echo -e "${YELLOW}⚠ PID $pid is inside a systemd-managed cgroup ($cg_path)${NC}"
        echo -e "${YELLOW}  systemd may move it back on the next reconciliation, or the move may${NC}"
        echo -e "${YELLOW}  silently no-op. If limits don't stick, this is almost always why.${NC}"
        return 0
    fi

    return 1
}

# Set memory limit for process
# mode: "swap"  = soft RAM cap; excess pages get swapped out, process keeps running
#       "hard"  = hard RAM cap; NO swap allowed, OOM-killed if exceeded
set_memory_limit() {
    local pid=$1
    local limit_mb=$2
    local mode=${3:-swap}

    local limit_bytes=$(( limit_mb * 1024 * 1024 ))
    local cmd
    cmd=$(ps -p "$pid" -o comm= 2>/dev/null || echo "unknown")
    local cgroup_ver
    cgroup_ver=$(detect_cgroup_version)

    check_systemd_owned "$pid" || true

    # Warn early if swap mode is requested but no swap exists
    if [[ "$mode" == "swap" ]]; then
        local swap_total
        swap_total=$(awk '/SwapTotal:/{print $2}' /proc/meminfo)
        if [[ "$swap_total" -eq 0 ]]; then
            echo -e "${YELLOW}⚠ No swap is enabled — push-to-swap will behave like a hard limit (OOM kill).${NC}"
            echo -e "${YELLOW}  To add swap:  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile${NC}"
            echo ""
        fi
    fi

    case "$cgroup_ver" in

        v2)
            local cg="/sys/fs/cgroup/ramlimit_${pid}"
            echo "+memory" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
            mkdir -p "$cg"

            if ! echo "$pid" > "${cg}/cgroup.procs" 2>/dev/null; then
                echo -e "${RED}✗ Could not move PID $pid to cgroup (may have exited)${NC}"
                rmdir "$cg" 2>/dev/null || true
                return 1
            fi

            if [[ "$mode" == "hard" ]]; then
                # Hard ceiling on RAM, zero swap allowed → OOM kill if exceeded
                echo "$limit_bytes" > "${cg}/memory.max"
                echo "0" > "${cg}/memory.swap.max" 2>/dev/null || \
                    echo -e "${YELLOW}⚠ memory.swap.max not available on this kernel${NC}"
            else
                # memory.high = soft limit: kernel starts reclaiming (swapping) when
                # usage crosses this, but the process is NOT killed.
                # memory.max stays at 'max' so there's no hard ceiling to OOM on.
                # memory.swap.max = max so swap space is unlimited for this cgroup.
                echo "$limit_bytes" > "${cg}/memory.high"
                echo "max"          > "${cg}/memory.max"
                echo "max"          > "${cg}/memory.swap.max" 2>/dev/null || true

                # Raise global swappiness if it's too low to bother swapping
                local swappiness
                swappiness=$(cat /proc/sys/vm/swappiness)
                if [[ $swappiness -lt 60 ]]; then
                    echo 60 > /proc/sys/vm/swappiness
                    echo -e "${YELLOW}ℹ vm.swappiness raised to 60 so the kernel will actually swap${NC}"
                fi
            fi
            ;;

        v1)
            local cg="/sys/fs/cgroup/memory/ramlimit_${pid}"
            mkdir -p "$cg"

            echo "$limit_bytes" > "${cg}/memory.limit_in_bytes"

            if [[ "$mode" == "hard" ]]; then
                # memsw == RAM limit → no swap headroom at all
                echo "$limit_bytes" > "${cg}/memory.memsw.limit_in_bytes" 2>/dev/null || \
                    echo -e "${YELLOW}⚠ memsw unavailable (CONFIG_MEMCG_SWAP disabled)${NC}"
                # Tell kernel not to swap for this cgroup
                echo "0" > "${cg}/memory.swappiness" 2>/dev/null || true
            else
                # memsw = 4× RAM limit → lots of swap headroom
                echo "$(( limit_bytes * 4 ))" > "${cg}/memory.memsw.limit_in_bytes" 2>/dev/null || \
                    echo -e "${YELLOW}⚠ memsw unavailable — swap allowance could not be set${NC}"
                # Aggressively prefer swapping over OOM-killing
                echo "80" > "${cg}/memory.swappiness" 2>/dev/null || true
            fi

            if ! echo "$pid" > "${cg}/cgroup.procs" 2>/dev/null; then
                echo -e "${RED}✗ Could not move PID $pid to cgroup (may have exited)${NC}"
                return 1
            fi
            ;;

        *)
            echo -e "${RED}✗ cgroups not available on this system${NC}"
            return 1
            ;;
    esac

    local limits
    limits=$(cat "$LIMITS_FILE")
    echo "$limits" | jq ".\"$pid\" = {\"cmd\": \"$cmd\", \"limit_mb\": $limit_mb, \"mode\": \"$mode\"}" > "$LIMITS_FILE"

    local mode_label
    [[ "$mode" == "hard" ]] && mode_label="Hard Limit (no swap)" || mode_label="Push to Swap"
    echo -e "${GREEN}✓ Set ${BOLD}${limit_mb}MB${NC}${GREEN} on PID $pid ($cmd) — ${BOLD}${mode_label}${NC}"
}

# Configure zswap
configure_zswap() {
    clear
    echo -e "${CYAN}${BOLD}=== ZSWAP CONFIGURATION ===${NC}\n"
    
    local zswap_enabled=$(cat /sys/module/zswap/parameters/enabled 2>/dev/null || echo "N")
    local compressor=$(cat /sys/module/zswap/parameters/compressor 2>/dev/null || echo "lz4")
    local max_pool_percent=$(cat /sys/module/zswap/parameters/max_pool_percent 2>/dev/null || echo "20")
    
    echo "Current Settings:"
    echo "  Enabled: $zswap_enabled"
    echo "  Compressor: $compressor"
    echo "  Max Pool %: $max_pool_percent"
    echo ""
    
    echo -e "${YELLOW}Options:${NC}"
    echo "1) Enable zswap"
    echo "2) Disable zswap"
    echo "3) Change compressor (lz4/zstd/deflate/lzo)"
    echo "4) Set max pool percent (1-50)"
    echo "5) Back to main menu"
    echo ""
    read -p "Select option: " choice
    
    case $choice in
        1)
            echo 1 > /sys/module/zswap/parameters/enabled
            echo -e "${GREEN}✓ zswap enabled${NC}"
            ;;
        2)
            echo 0 > /sys/module/zswap/parameters/enabled
            echo -e "${GREEN}✓ zswap disabled${NC}"
            ;;
        3)
            # Only compressors actually compiled into the running kernel work here.
            # Check crypto/comp registry for what's really available before writing,
            # instead of blind-trusting the typed string and failing silently.
            local available_comps
            available_comps=$(grep -A1 '^name' /proc/crypto 2>/dev/null | grep -oE '(lz4hc|lz4|zstd|deflate|lzo-rle|lzo|842)' | sort -u | tr '\n' ' ')
            if [[ -z "$available_comps" ]]; then
                available_comps="unknown (could not read /proc/crypto)"
            fi
            echo "Detected available compressors: $available_comps"
            read -p "Enter compressor (lz4/zstd/deflate/lzo): " comp

            if [[ -n "$available_comps" ]] && [[ ! " $available_comps " == *" $comp "* ]]; then
                echo -e "${YELLOW}⚠ '$comp' doesn't appear to be compiled into this kernel — trying anyway${NC}"
            fi

            if echo "$comp" > /sys/module/zswap/parameters/compressor 2>/dev/null; then
                echo -e "${GREEN}✓ Compressor set to $comp${NC}"
            else
                echo -e "${RED}✗ Failed to set compressor — kernel likely doesn't have '$comp' built/loaded${NC}"
            fi
            ;;
        4)
            read -p "Enter pool percent (1-50): " percent
            if ! [[ "$percent" =~ ^[0-9]+$ ]] || (( percent < 1 || percent > 100 )); then
                echo -e "${RED}✗ Invalid percent (must be 1-100)${NC}"
            elif echo "$percent" > /sys/module/zswap/parameters/max_pool_percent 2>/dev/null; then
                echo -e "${GREEN}✓ Pool percent set to $percent%${NC}"
            else
                echo -e "${RED}✗ Failed to set pool percent${NC}"
            fi
            ;;
        5)
            return
            ;;
    esac
    
    sleep 1.5
}

# Configure swap/disk strategy
configure_swap_strategy() {
    clear
    echo -e "${CYAN}${BOLD}=== SWAP STRATEGY CONFIGURATION ===${NC}\n"
    
    local config=$(cat "$CONFIG_FILE")
    local disk_ratio=$(echo "$config" | jq '.disk_swap_ratio')
    local zswap_ratio=$(echo "$config" | jq '.zswap_ratio')
    
    echo "Current Split Ratio:"
    echo -e "  ${MAGENTA}Disk Swap:${NC} ${BOLD}${disk_ratio}%${NC}"
    echo -e "  ${MAGENTA}Zswap:${NC}     ${BOLD}${zswap_ratio}%${NC}"
    echo ""
    
    echo -e "${YELLOW}Presets:${NC}"
    echo "1) Favor Disk (70/30)      - More disk, less memory"
    echo "2) Balanced (50/50)        - Equal split"
    echo "3) Favor Zswap (30/70)    - Fast in-memory compression"
    echo "4) Max Memory (10/90)      - Minimize disk I/O"
    echo "5) Custom ratio"
    echo "6) Back to main menu"
    echo ""
    read -p "Select option: " choice
    
    case $choice in
        1)
            update_config '.disk_swap_ratio = 70 | .zswap_ratio = 30'
            apply_swap_limits 70 30
            echo -e "${GREEN}✓ Favoring disk swap (70/30)${NC}"
            ;;
        2)
            update_config '.disk_swap_ratio = 50 | .zswap_ratio = 50'
            apply_swap_limits 50 50
            echo -e "${GREEN}✓ Balanced mode (50/50)${NC}"
            ;;
        3)
            update_config '.disk_swap_ratio = 30 | .zswap_ratio = 70'
            apply_swap_limits 30 70
            echo -e "${GREEN}✓ Favoring zswap (30/70)${NC}"
            ;;
        4)
            update_config '.disk_swap_ratio = 10 | .zswap_ratio = 90'
            apply_swap_limits 10 90
            echo -e "${GREEN}✓ Max memory mode (10/90)${NC}"
            ;;
        5)
            read -p "Disk swap % (0-100): " disk
            local zswap=$((100 - disk))
            update_config ".disk_swap_ratio = $disk | .zswap_ratio = $zswap"
            apply_swap_limits "$disk" "$zswap"
            echo -e "${GREEN}✓ Custom ratio set (${disk}/${zswap})${NC}"
            ;;
        6)
            return
            ;;
    esac
    
    sleep 1.5
}

# Apply swap limits
# disk_percent / zswap_percent are a *ratio*, not independent absolute sizes:
#   - zswap_percent maps directly onto zswap's max_pool_percent (RAM used for
#     the compressed cache, as a % of total RAM — that's what the kernel knob is)
#   - disk_percent actually resizes the on-disk swapfile, so it's a real change,
#     not a printed number. Only touches FILE-based swap (never a raw partition).
apply_swap_limits() {
    local disk_percent=$1
    local zswap_percent=$2

    # --- zswap portion: fix from 1.0 — max_pool_percent wants a plain percent,
    # not a byte count. Just pass it through directly. ---
    if [[ -d /sys/module/zswap ]]; then
        if echo "$zswap_percent" > /sys/module/zswap/parameters/max_pool_percent 2>/dev/null; then
            echo -e "${GREEN}✓ zswap max_pool_percent set to ${zswap_percent}%${NC}"
        else
            echo -e "${RED}✗ Failed to set zswap max_pool_percent${NC}"
        fi
    else
        echo -e "${YELLOW}⚠ zswap module not loaded — skipping zswap portion${NC}"
    fi

    # --- disk portion: find an active FILE-backed swap to resize ---
    local swapfile
    swapfile=$(swapon --show=NAME,TYPE --noheadings 2>/dev/null | awk '$2=="file"{print $1; exit}')

    if [[ -z "$swapfile" ]]; then
        echo -e "${YELLOW}⚠ No file-backed swap found (only partition swap, or no swap at all)${NC}"
        echo -e "${YELLOW}  Disk-swap ratio can't be applied — partition swap isn't safely resizable here.${NC}"
        return
    fi

    local total_swap_kb
    total_swap_kb=$(free | awk '/Swap:/{print $2}')
    local target_disk_kb=$(( total_swap_kb * disk_percent / 100 ))
    local target_disk_mb=$(( target_disk_kb / 1024 ))

    if (( target_disk_mb < 64 )); then
        echo -e "${YELLOW}⚠ Target swapfile size (${target_disk_mb}MB) is too small to be useful — skipping resize${NC}"
        return
    fi

    echo -e "${YELLOW}This will swapoff, resize, and swapon: ${swapfile} → ${target_disk_mb}MB${NC}"
    echo -e "${YELLOW}Swap will be briefly unavailable during the resize — make sure there's enough free RAM right now.${NC}"
    read -p "Proceed? (yes/no): " confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "Skipped disk swap resize."
        return
    fi

    if ! swapoff "$swapfile" 2>/dev/null; then
        echo -e "${RED}✗ Could not swapoff $swapfile (in use / not enough free RAM to absorb it right now)${NC}"
        return
    fi

    if fallocate -l "${target_disk_mb}M" "$swapfile" 2>/dev/null || dd if=/dev/zero of="$swapfile" bs=1M count="$target_disk_mb" status=none; then
        chmod 600 "$swapfile"
        mkswap "$swapfile" >/dev/null
        swapon "$swapfile"
        echo -e "${GREEN}✓ Resized $swapfile to ${target_disk_mb}MB and re-enabled${NC}"
    else
        echo -e "${RED}✗ Resize failed — attempting to re-enable swap at old size to avoid leaving system without swap${NC}"
        swapon "$swapfile" 2>/dev/null || echo -e "${RED}✗ Could not re-enable swap — check $swapfile manually${NC}"
    fi
}

# Update JSON config
update_config() {
    local update=$1
    local config=$(cat "$CONFIG_FILE")
    echo "$config" | jq "$update" > "$CONFIG_FILE"
}

# Interactive process limiter
interactive_limiter() {
    clear
    echo -e "${CYAN}${BOLD}=== INTERACTIVE PROCESS LIMITER ===${NC}\n"
    
    show_top_processes 15
    echo ""
    
    read -p "Enter PID to limit (or 'back'): " pid_input
    
    if [[ "$pid_input" == "back" ]]; then
        return
    fi
    
    if ! [[ "$pid_input" =~ ^[0-9]+$ ]]; then
        echo -e "${RED}✗ Invalid PID${NC}"
        sleep 1
        return
    fi
    
    # Get current RAM usage of process
    local ram_mb=$(($(ps -p "$pid_input" -o rss= 2>/dev/null || echo 0) / 1024))
    local cmd=$(ps -p "$pid_input" -o comm= 2>/dev/null || echo "Unknown")
    
    echo -e "\n${BOLD}Process:${NC} $cmd (PID: $pid_input)"
    echo -e "${BOLD}Current RAM:${NC} ${ram_mb}MB"
    echo ""
    
    read -p "Set limit (MB) [default: $(($ram_mb / 2))]: " limit_input
    limit_input=${limit_input:-$((ram_mb / 2))}
    
    if [[ "$limit_input" =~ ^[0-9]+$ ]] && [[ $limit_input -gt 10 ]]; then
        echo ""
        echo -e "${YELLOW}Limiting mode:${NC}"
        echo "  1) Push to Swap  — RAM is capped; excess pages spill to swap (process keeps running)"
        echo "  2) Hard Limit    — RAM is capped; NO swap allowed (OOM-killed if it goes over)"
        echo ""
        read -p "Select mode [1/2, default: 1]: " mode_choice
        mode_choice=${mode_choice:-1}

        local mode
        case "$mode_choice" in
            1) mode="swap" ;;
            2) mode="hard" ;;
            *)
                echo -e "${RED}✗ Invalid mode, defaulting to Push to Swap${NC}"
                mode="swap"
                ;;
        esac

        set_memory_limit "$pid_input" "$limit_input" "$mode"
    else
        echo -e "${RED}✗ Invalid limit (minimum 10MB)${NC}"
    fi
    
    sleep 1.5
}

# Monitor running limits
monitor_limits() {
    clear
    echo -e "${CYAN}${BOLD}=== MONITORING ACTIVE LIMITS ===${NC}\n"
    
    local limits=$(cat "$LIMITS_FILE")
    
    if [[ "$limits" == "{}" ]] || [[ -z "$limits" ]]; then
        echo -e "${YELLOW}No active limits set${NC}"
        sleep 1
        return
    fi
    
    echo -e "${BOLD}PID         Command              Limit(MB)  Current(MB)  Mode              Status${NC}"
    echo "─────────────────────────────────────────────────────────────────────────────────"
    
    echo "$limits" | jq -r 'to_entries | .[] | "\(.key) \(.value.cmd) \(.value.limit_mb) \(.value.mode // "swap")"' | while read pid cmd limit mode; do
        local current=$(ps -p "$pid" -o rss= 2>/dev/null || echo 0)
        current=$((current / 1024))

        local mode_label
        [[ "$mode" == "hard" ]] && mode_label="Hard (no swap)" || mode_label="Push to Swap"

        local status
        if ! ps -p "$pid" > /dev/null 2>&1; then
            status="${RED}DEAD${NC}"
        elif [[ $current -gt $limit ]]; then
            status="${RED}OVER${NC}"
        else
            status="${GREEN}OK${NC}"
        fi
        
        printf "%-11s %-20s %-10d %-12d %-17s %b\n" "$pid" "$cmd" "$limit" "$current" "$mode_label" "$status"
    done
    
    echo ""
    read -p "Press Enter to continue..."
}

# System status dashboard
show_dashboard() {
    clear
    echo -e "${CYAN}${BOLD}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}${BOLD}║          RAM LIMITER - SYSTEM DASHBOARD                    ║${NC}"
    echo -e "${CYAN}${BOLD}╚════════════════════════════════════════════════════════════╝${NC}\n"
    
    get_system_info
    echo ""
    
    show_top_processes 5
    
    echo -e "\n${CYAN}=== SWAP STATUS ===${NC}"
    swapon --show 2>/dev/null || echo "No swap configured"
    
    echo -e "\n${CYAN}=== ZSWAP STATUS ===${NC}"
    if [[ -d /sys/module/zswap ]]; then
        echo "Enabled: $(cat /sys/module/zswap/parameters/enabled)"
        echo "Compressor: $(cat /sys/module/zswap/parameters/compressor)"
    else
        echo "zswap module not loaded"
    fi
}

# Main menu
main_menu() {
    while true; do
        clear
        echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════╗${NC}"
        echo -e "${CYAN}${BOLD}║      RAM LIMITER - MAIN MENU             ║${NC}"
        echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════╝${NC}\n"
        
        echo -e "${YELLOW}MONITORING:${NC}"
        echo "  1) Show Dashboard"
        echo "  2) Top Memory Processes"
        echo "  3) Monitor Active Limits"
        
        echo -e "\n${YELLOW}CONFIGURATION:${NC}"
        echo "  4) Limit Process by PID"
        echo "  5) Configure zswap"
        echo "  6) Configure Swap Strategy"
        
        echo -e "\n${YELLOW}UTILITIES:${NC}"
        echo "  7) Clear All Limits"
        echo "  8) System Info"
        echo "  0) Exit"
        
        echo ""
        read -p "Select option: " choice
        
        case $choice in
            1) show_dashboard; read -p "Press Enter..."; ;;
            2) show_top_processes 20; read -p "Press Enter..."; ;;
            3) monitor_limits; ;;
            4) interactive_limiter; ;;
            5) configure_zswap; ;;
            6) configure_swap_strategy; ;;
            7) 
                read -p "Clear ALL limits? (yes/no): " confirm
                if [[ "$confirm" == "yes" ]]; then
                    local cg_ver
                    cg_ver=$(detect_cgroup_version)
                    if [[ "$cg_ver" == "v2" ]]; then
                        for cg in /sys/fs/cgroup/ramlimit_*/; do
                            [[ -d "$cg" ]] || continue
                            # Move all procs back to root cgroup before removing
                            while read -r p; do
                                echo "$p" > /sys/fs/cgroup/cgroup.procs 2>/dev/null || true
                            done < "${cg}cgroup.procs" 2>/dev/null
                            rmdir "$cg" 2>/dev/null || true
                        done
                    else
                        rm -rf /sys/fs/cgroup/memory/ramlimit_*
                    fi
                    echo '{}' > "$LIMITS_FILE"
                    echo -e "${GREEN}✓ All limits cleared${NC}"
                    sleep 1
                fi
                ;;
            8) echo -e "\n$(get_system_info)\n"; read -p "Press Enter..."; ;;
            0) 
                echo -e "${YELLOW}Exiting RAM Limiter...${NC}"
                exit 0
                ;;
            *)
                echo -e "${RED}✗ Invalid option${NC}"
                sleep 1
                ;;
        esac
    done
}

# Cleanup on exit
cleanup() {
    echo -e "${YELLOW}Cleaning up...${NC}"
}

trap cleanup EXIT

# Main execution
main() {
    check_root
    init_config
    main_menu
}

# Run main
main "$@"
