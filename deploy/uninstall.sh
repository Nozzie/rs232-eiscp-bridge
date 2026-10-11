#!/usr/bin/env bash
#
# Uninstall the Onkyo RS-232 -> eISCP bridge installed by deploy/install.sh.
#
# Reverts exactly what install.sh does:
#   - stops and disables the onkyo-bridge system service
#   - removes /etc/systemd/system/onkyo-bridge.service (and any stale wants/ symlink)
#   - removes /opt/onkyo-bridge
#   - removes /etc/default/onkyo-bridge
#   - deletes the onkyo-bridge system user (which also clears its dialout membership)
#
# Usage:
#   sudo ./deploy/uninstall.sh                 # full removal
#   sudo ./deploy/uninstall.sh --dry-run       # show what would be removed
#   sudo ./deploy/uninstall.sh --keep-user     # leave the onkyo-bridge user
#   sudo ./deploy/uninstall.sh --keep-config   # leave /etc/default/onkyo-bridge
#
# Safe to run when nothing is installed, and safe to run twice.
#
set -euo pipefail

# See install.sh: sudo may preserve the caller's PATH (OSMC ships
# sudoers.d/osmc-no-secure-path) and these tools live in /usr/sbin.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

SERVICE="onkyo-bridge.service"
UNIT="/etc/systemd/system/onkyo-bridge.service"
PREFIX="/opt/onkyo-bridge"
CONF="/etc/default/onkyo-bridge"
USER_NAME="onkyo-bridge"
PORT="60128"

DRY_RUN=0
KEEP_USER=0
KEEP_CONFIG=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Uninstall the Onkyo RS-232 -> eISCP bridge.

Usage: sudo ./deploy/uninstall.sh [options]

Options:
  -n, --dry-run     Show what would be removed without changing anything
      --keep-user   Do not delete the onkyo-bridge system user
      --keep-config Do not delete /etc/default/onkyo-bridge
  -h, --help        Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run)   DRY_RUN=1 ;;
        --keep-user)    KEEP_USER=1 ;;
        --keep-config)  KEEP_CONFIG=1 ;;
        -h|--help)      usage; exit 0 ;;
        *)              usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

for tool in systemctl rm find; do
    command -v "$tool" >/dev/null 2>&1 || die "required command not found: $tool"
done

[[ $EUID -eq 0 ]] || die "must run as root (use sudo)"

# Run a command, or just print it when dry-running.
act() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '    would run: %s\n' "$*"
    else
        "$@"
    fi
}

if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY RUN - nothing will be changed"
fi

# --- 1. stop and disable the service -----------------------------------------
# Done before the unit file is removed, so systemd can unwind its wants/ symlink.
if [[ -f $UNIT ]] || systemctl list-unit-files "$SERVICE" >/dev/null 2>&1; then
    log "Stopping and disabling $SERVICE"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '    would run: systemctl disable --now %s\n' "$SERVICE"
    else
        systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
    fi
else
    log "$SERVICE is not installed"
fi

# --- 2. remove the unit file and any stale enablement symlink ----------------
if [[ -f $UNIT ]]; then
    log "Removing $UNIT"
    act rm -f "$UNIT"
else
    log "$UNIT already absent"
fi

for link in /etc/systemd/system/*.wants/"$SERVICE"; do
    # Unmatched globs stay literal; skip those.
    [[ -L $link ]] || continue
    log "Removing stale symlink $link"
    act rm -f "$link"
done

if [[ $DRY_RUN -eq 0 ]]; then
    log "Reloading systemd"
    systemctl daemon-reload || true
    systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
fi

# --- 3. remove the installed program -----------------------------------------
if [[ -d $PREFIX ]]; then
    # Defensive: this is an rm -rf, so never let it target anything unexpected.
    [[ "$PREFIX" == "/opt/onkyo-bridge" ]] || die "refusing to remove unexpected path: $PREFIX"
    log "Removing $PREFIX"
    act rm -rf "$PREFIX"
else
    log "$PREFIX already absent"
fi

# --- 4. remove the config ----------------------------------------------------
if [[ -f $CONF ]]; then
    if [[ $KEEP_CONFIG -eq 1 ]]; then
        log "Keeping $CONF (--keep-config)"
    else
        log "Removing $CONF"
        act rm -f "$CONF"
    fi
else
    log "$CONF already absent"
fi

# --- 5. remove the system user -----------------------------------------------
if id "$USER_NAME" >/dev/null 2>&1; then
    if [[ $KEEP_USER -eq 1 ]]; then
        log "Keeping system user $USER_NAME (--keep-user)"
    else
        # userdel also removes the user from any supplementary groups (dialout).
        log "Removing system user $USER_NAME"
        act userdel "$USER_NAME"
    fi
else
    log "System user $USER_NAME not present"
fi

# --- 6. verify ---------------------------------------------------------------
echo
log "Verification"
PROBLEM=0

report_removed() {
    local path="$1"
    if [[ -e $path || -L $path ]]; then
        printf '  %-40s STILL PRESENT\n' "$path"
        PROBLEM=1
    else
        printf '  %-40s removed\n' "$path"
    fi
}

report_removed "$UNIT"
report_removed "$PREFIX"

if [[ $KEEP_CONFIG -eq 1 ]]; then
    printf '  %-40s kept (--keep-config)\n' "$CONF"
else
    report_removed "$CONF"
fi

if [[ $KEEP_USER -eq 1 ]]; then
    printf '  %-40s kept (--keep-user)\n' "user $USER_NAME"
elif id "$USER_NAME" >/dev/null 2>&1; then
    printf '  %-40s STILL PRESENT\n' "user $USER_NAME"
    PROBLEM=1
else
    printf '  %-40s removed\n' "user $USER_NAME"
fi

if [[ $DRY_RUN -eq 0 ]]; then
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        printf '  %-40s STILL ACTIVE\n' "$SERVICE"
        PROBLEM=1
    else
        printf '  %-40s not active\n' "$SERVICE"
    fi

    LISTENERS="$(ss -lntup 2>/dev/null | grep -c ":$PORT" || true)"
    if [[ ${LISTENERS:-0} -gt 0 ]]; then
        printf '  %-40s %s listener(s) still up\n' "port $PORT" "$LISTENERS"
        PROBLEM=1
    else
        printf '  %-40s free\n' "port $PORT"
    fi
fi

# The system service is not the only way this can run: deploy/onkyo-bridge.user.service
# installs a systemd --user unit for a single user. Flag it rather than silently
# reaching into another user's home directory.
LEFTOVER="$(pgrep -af onkyo_serial_bridge 2>/dev/null || true)"
if [[ -n $LEFTOVER ]]; then
    echo
    warn "a bridge process is still running:"
    printf '    %s\n' "$LEFTOVER"
    warn "this is probably a per-user service. Stop it as that user with:"
    warn "    systemctl --user disable --now onkyo-bridge"
    PROBLEM=1
fi

echo
if [[ $DRY_RUN -eq 1 ]]; then
    log "Dry run complete - nothing was changed."
elif [[ $PROBLEM -eq 0 ]]; then
    log "Uninstall complete."
else
    warn "Uninstall finished, but some items remain (see above)."
    exit 1
fi
