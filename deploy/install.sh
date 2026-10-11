#!/usr/bin/env bash
#
# Install the Onkyo RS-232 -> eISCP bridge on this host.
# Run as root from the directory containing onkyo_serial_bridge.py.
#
#   sudo ./deploy/install.sh
#
set -euo pipefail

# sudo does not always reset PATH: OSMC ships a sudoers drop-in named
# "osmc-no-secure-path", so the caller's PATH is preserved. useradd/usermod live
# in /usr/sbin, which is not on a normal user's PATH, so pin a known-good one.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="/opt/onkyo-bridge"
CONF="/etc/default/onkyo-bridge"
UNIT="/etc/systemd/system/onkyo-bridge.service"
USER_NAME="onkyo-bridge"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Preflight: fail early with a clear message rather than midway through.
for tool in install useradd usermod systemctl find awk sed; do
    command -v "$tool" >/dev/null 2>&1 || die "required command not found: $tool"
done

[[ $EUID -eq 0 ]] || die "must run as root (use sudo)"
[[ -f "$SRC_DIR/onkyo_serial_bridge.py" ]] || die "onkyo_serial_bridge.py not found in $SRC_DIR"

# --- serial device -----------------------------------------------------------
log "Detecting serial device"
DEVICE=""
if [[ -d /dev/serial/by-id ]]; then
    mapfile -t CANDIDATES < <(find /dev/serial/by-id -maxdepth 1 -type l 2>/dev/null | sort)
    if [[ ${#CANDIDATES[@]} -eq 1 ]]; then
        DEVICE="${CANDIDATES[0]}"
        log "Found: $DEVICE"
    elif [[ ${#CANDIDATES[@]} -gt 1 ]]; then
        printf 'Multiple serial adapters found:\n'
        printf '  %s\n' "${CANDIDATES[@]}"
        read -rp "Enter the full path to use for the Onkyo: " DEVICE
    fi
fi
if [[ -z "$DEVICE" ]]; then
    if [[ -e /dev/ttyUSB0 ]]; then
        DEVICE="/dev/ttyUSB0"
        log "Falling back to $DEVICE (prefer a /dev/serial/by-id path)"
    else
        die "no serial device found. Plug the RS-232 adapter in and re-run.
If the cable is attached but not detected, check 'dmesg | tail' for a driver
issue (FTDI/PL2303 adapters need their kernel module)."
    fi
fi

# --- user --------------------------------------------------------------------
if id "$USER_NAME" &>/dev/null; then
    log "User $USER_NAME already exists"
else
    log "Creating system user $USER_NAME"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
fi
log "Adding $USER_NAME to the dialout group"
usermod -aG dialout "$USER_NAME"

# --- files -------------------------------------------------------------------
log "Installing to $PREFIX"
install -d -m 0755 "$PREFIX"
install -m 0644 "$SRC_DIR/onkyo_serial_bridge.py" "$PREFIX/onkyo_serial_bridge.py"
[[ -f "$SRC_DIR/README.md" ]] && install -m 0644 "$SRC_DIR/README.md" "$PREFIX/README.md"

if [[ -f "$CONF" ]]; then
    log "$CONF exists; leaving it alone"
else
    log "Writing $CONF"
    install -d -m 0755 "$(dirname "$CONF")"
    MAC="$(cat /sys/class/net/$(ip route show default | awk '/default/{print $5; exit}')/address 2>/dev/null \
           | tr -d ':' | tr '[:lower:]' '[:upper:]' || true)"
    [[ -n "${MAC:-}" ]] || MAC="000000000001"
    sed -e "s|^ONKYO_DEVICE=.*|ONKYO_DEVICE=${DEVICE}|" \
        -e "s|^ONKYO_IDENTIFIER=.*|ONKYO_IDENTIFIER=${MAC:0:12}|" \
        "$SRC_DIR/deploy/onkyo-bridge.default" > "$CONF"
    chmod 0644 "$CONF"
fi

# --- service -----------------------------------------------------------------
log "Installing systemd unit"
install -d -m 0755 "$(dirname "$UNIT")"
install -m 0644 "$SRC_DIR/deploy/onkyo-bridge.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now onkyo-bridge.service
sleep 2

# --- verify ------------------------------------------------------------------
log "Service status"
systemctl --no-pager --lines=15 status onkyo-bridge.service || true

echo
log "Listening sockets:"
ss -lntup 2>/dev/null | grep -E '60128' || echo "  (nothing on 60128 yet - check the log above)"

# Best-effort local IP for the closing hints. `hostname -I` is a net-tools /
# util-linux extension and is absent from GNU inetutils, so fall back to ip(8).
# The `|| true` guards are required: `set -o pipefail` would otherwise abort the
# script when hostname rejects the unknown option.
HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
if [[ -z "$HOSTIP" ]]; then
    HOSTIP="$(ip -4 route get 1.1.1.1 2>/dev/null \
              | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}' || true)"
fi
[[ -n "$HOSTIP" ]] || HOSTIP="<this-host-ip>"

cat <<EOF

Done.

  Config:  $CONF   (edit, then: sudo systemctl restart onkyo-bridge)
  Logs:    journalctl -u onkyo-bridge -f

Add it to Home Assistant as an Onkyo integration pointed at this host:
  Settings > Devices & services > Add integration > Onkyo > $HOSTIP

If Home Assistant cannot reach port 60128, check the firewall:
  sudo ufw allow from <ha-ip> to any port 60128 proto tcp
  sudo ufw allow from <ha-ip> to any port 60128 proto udp
EOF
