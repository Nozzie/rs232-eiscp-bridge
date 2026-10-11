# Onkyo RS-232 to eISCP bridge

Control an RS-232-only (or network-dead) Onkyo/Integra receiver from Home
Assistant over the network, by turning a Linux box's serial port into a virtual
network receiver.

```
   HA host                    bridge host                     Onkyo AVR
 ┌──────────────┐            ┌──────────────────┐            ┌───────────┐
 │ Onkyo integr.│  TCP/UDP   │  bridge daemon   │  RS-232    │  serial   │
 │ (built-in)   │◄──60128───►│  owns the port   │◄──────────►│   port    │
 └──────────────┘            └──────────────────┘  9600 8N1  └───────────┘
```

Throughout this document `192.168.1.50` stands for the Linux host running the
bridge and `192.168.1.10` for the Home Assistant host. Substitute your own.

## Why a bridge is needed

Home Assistant's built-in [Onkyo integration](https://www.home-assistant.io/integrations/onkyo/)
only speaks **eISCP over TCP** (host + port `60128`). It has no serial
transport, so it cannot talk to `/dev/ttyUSB0` directly, and it cannot reach a
serial port on a different machine.

The good news is that eISCP *is* ISCP (the RS-232 protocol) with a 16-byte
header prepended, and the command text is identical:

| | Bytes |
|---|---|
| Raw serial command | `!1DIM02\r` |
| Same command inside eISCP | `ISCP` + header + `!1DIM02\r\n` |

So this bridge is a thin, well-defined translation layer rather than a
reimplementation of the protocol.

An RS-232 receiver also *pushes* unsolicited status when anything changes
(power, volume, input). Holding the port open captures those and feeds them to
HA, which is why the integration ends up working as a proper `media_player`
with live state instead of write-only buttons.

## Requirements

- A Linux box with the receiver attached by RS-232.
- **Python 3.9+.** No third-party packages: the daemon uses only the standard
  library (`termios`, `asyncio`).
- Home Assistant reachable on the same network, with a free TCP/UDP port
  `60128` on the Linux box.
- An RS-232 cable that actually works bidirectionally. Verify before
  installing — see [Verifying the serial link](#verifying-the-serial-link).

> Only one process can hold the serial port while the bridge runs. The daemon
> takes exclusive access (`TIOCEXCL`), so the ad-hoc `printf > /dev/ttyUSB0`
> trick will start failing with `EBUSY` — that is expected. Stop the service to
> go back to manual commands.

## Install

```bash
# copy this directory to the bridge host, then:
sudo ./deploy/install.sh
```

The script detects the serial adapter, creates an `onkyo-bridge` system user,
installs to `/opt/onkyo-bridge`, writes `/etc/default/onkyo-bridge`, and
enables a systemd service. It needs root and fails early with a clear message if
a required tool is missing.

To undo everything it did, use the matching uninstaller — see
[Uninstall](#uninstall).

Edit the config if needed and restart:

```bash
sudo nano /etc/default/onkyo-bridge
sudo systemctl restart onkyo-bridge
journalctl -u onkyo-bridge -f
```

Useful checks:

```bash
systemctl status onkyo-bridge
ss -lntup | grep 60128                 # expect TCP and UDP listeners
```

## Installing without root (systemd user service)

If you have no root on the bridge host, install the same daemon as a **systemd
user service** instead of a system service. The account needs to be able to open
the serial port (typically by being in the `dialout` group).

```bash
mkdir -p ~/.config/systemd/user
cp deploy/onkyo-bridge.user.service ~/.config/systemd/user/
# edit it first: ExecStart path, device path, model name, identifier
systemctl --user daemon-reload
systemctl --user enable --now onkyo-bridge
```

An unprivileged install cannot use `/opt` or `/etc/default`, so the user unit
carries the device path and options inline rather than reading the environment
file. The bundled unit expects the script at `~/onkyo-bridge/onkyo_serial_bridge.py`
and its example device path is one adapter's — adjust both.

### Enable lingering

This matters more than it looks. Without it, systemd stops the user manager —
and with it every user service — as soon as the last login session for that user
ends. That presents as a bridge that works while you are SSH'd in and is gone
afterwards, with HA seeing `Connection refused`. With lingering enabled the unit
starts at boot, independent of logins:

```bash
sudo loginctl enable-linger <user>
```

```bash
# status / control (as the bridge user)
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user status onkyo-bridge
systemctl --user restart onkyo-bridge
journalctl --user -u onkyo-bridge -f      # may need group systemd-journal
```

Some minimal distributions do not let a user read the journal at all, so
`journalctl --user` may be denied; `systemctl --user status` still shows the
last few lines, and the daemon logs to stderr.

The root-level install (`deploy/onkyo-bridge.service` + `deploy/install.sh`) is
the option to prefer on a normal Debian/Ubuntu host where sudo is available.

## Home Assistant setup

1. **Settings → Devices & services → Add integration → Onkyo**
2. Host: `192.168.1.50` (the *Linux box*, not the receiver)
3. Volume resolution: match your receiver — **`200` on most models**; see the
   note below
4. Pick your input sources and listening modes.

### Set the volume resolution correctly

This is easy to get wrong and produces a receiver that jumps to the wrong
volume. HA asks for a **volume resolution**, which is the receiver's raw volume
ceiling: it divides the reported value by that number to get a `media_player`
volume from 0 to 1. Pick the wrong one and HA's 100% lands on the wrong point on
the dial.

Onkyo reports volume as the `MVL` parameter in **hexadecimal**. On most models
the range is `0x00`–`0xC8` (0–200 decimal), so `200` is the correct answer:

```
!1MVL19   ->  hex 0x19  ->  25
!1MVLC8   ->  hex 0xC8  ->  200   (range maximum)
```

So a raw `!1MVL19` correctly shows up in Home Assistant as `25`, not `19`. If
your receiver's ceiling is lower, pick the matching value — the config flow
offers `50`, `80`, `100`, and `200`.

HA finds the receiver through the bridge's UDP discovery responder, so
auto-discovery should also work.

### Firewall

HA must reach **both** TCP and UDP 60128. UDP is easy to overlook and its
absence breaks setup with a confusing "no devices found":

```bash
sudo ufw allow from 192.168.1.10 to any port 60128 proto tcp
sudo ufw allow from 192.168.1.10 to any port 60128 proto udp
```

## Verifying the serial link

`printf` alone only proves the write path succeeded — it is silent about
whether the receiver understood anything. Confirm bidirectional comms with:

```bash
sudo systemctl stop onkyo-bridge        # release exclusive access
stty -F /dev/ttyUSB0 9600 cs8 -cstopb -parenb -crtscts -ixon -ixoff clocal raw -echo
timeout 3 cat /dev/ttyUSB0 &
sleep 0.3
printf '!1PWRQSTN\r' > /dev/ttyUSB0
wait
```

Expect a reply like `!1PWR01` (on) or `!1PWR00` (standby). **Silence here means
the bridge cannot work either** — check the cable (many Onkyo models want a
null-modem / crossed cable), that your user is in the `dialout` group, and that
`dmesg` shows the adapter.

## Testing

```bash
# Bridge protocol tests: pseudo-terminal as a fake receiver, driven by the real
# aioonkyo library (the one Home Assistant uses).
python3 -m venv .venv && .venv/bin/pip install aioonkyo
.venv/bin/python tests/e2e_test.py

# Verify a running bridge from another machine (read-only queries).
.venv/bin/python tests/remote_check.py 192.168.1.50

# Deploy-script round trip: no root, no system changes, privileged commands stubbed.
./tests/test_deploy_scripts.sh
```

`e2e_test.py` covers discovery, interview, control, status, and the filtering
rules described below.

`test_deploy_scripts.sh` copies `install.sh` and `uninstall.sh` into a scratch
tree with `systemctl`/`useradd`/`usermod`/`userdel`/`id` replaced by
absolute-path stubs that keep their own state, so `useradd`→`userdel` and
`systemctl enable`→`disable` behave realistically. It then checks that install
sets everything up, that `--dry-run` changes nothing, that a full uninstall
reverts the tree exactly, that a second uninstall is harmless, and that
`--keep-user`/`--keep-config` are honoured.

## Protocol notes

Details that are easy to get wrong; each is enforced by the daemon and covered
by the test suite.

- **Instruction terminator.** HA sends `!1PWRQSTN\r\n`; the receiver wants a
  bare `\r`. The bridge strips and re-terminates.
- **Status terminator.** Hardware may end a line with `\x1a`, `\r`, `\n`, or a
  combination. `aioonkyo` only accepts `\x1a\r\n` or `\r\n` on the TCP side,
  so the bridge normalises to `\x1a\r\n`.
- **Discovery is UDP, not TCP,** and it is mandatory even for *manual* setup:
  HA calls `aioonkyo.interview(host)`, which sends an eISCP discovery query
  over UDP and waits for a reply. The bridge answers it directly using the
  format `!1ECN<model>/<port>/DX/<identifier>` terminated by `\x19\r\n`, with
  an identifier of 1–12 characters. (A trailing empty identifier, which real
  hardware sometimes sends, is rejected by `aioonkyo`.)
- **`ECN` replies from the serial port are deliberately not forwarded.** Their
  serial form has no identifier field, so forwarding one would raise a parse
  error in `aioonkyo` and tear down the connection. The bridge intercepts them
  and uses them only to learn the real model name.
- **Model name is learned, not assumed.** `--model` is the fallback advertised
  before the receiver answers `!1ECNQSTN`; once it answers, the real name is
  used.

### Observed on hardware

Measured on a development receiver while building the bridge, and the reason two
design choices above are not optional:

| Query sent | Reply | Note |
|---|---|---|
| `!1PWRQSTN` | `!1PWR01\x1a` | power on |
| `!1MVLQSTN` | `!1MVL19\x1a` | volume: hex `19` = 25 |
| `!1SLIQSTN` | `!1SLI11\x1a` | input 11 |
| `!1ECNQSTN` | *(no reply)* | **model query not answered over RS-232** |

Two consequences, both of which hold whether or not your receiver behaves the
same way:

1. **The terminator may be a bare `\x1a`** — no CR, no LF, on at least some
   models. Forwarding bytes verbatim would make `aioonkyo` reject every status
   message, so the bridge normalises the ending instead of trusting it.
2. **Discovery cannot rely on the receiver.** At least one model does not answer
   `ECN` over serial, so a bridge that queried the receiver for its identity
   would fail at setup with a discovery timeout. The bridge therefore answers
   discovery itself and uses `--model` as the advertised name.

## Options

```
--device PATH     Serial device. Globs allowed, e.g.
                  /dev/serial/by-id/usb-FTDI_*-if00-port0
--baud N          1200..115200 (default 9600)
--bind ADDR       Bind address (default 0.0.0.0)
--port N          eISCP port (default 60128)
--model NAME      Model advertised during discovery
--identifier STR  Up to 12 chars advertised during discovery (default: host MAC)
--no-learn-model  Do not query the receiver for its model name
-v, --verbose     Debug logging
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Nothing found` / discovery timeout in HA | UDP 60128 blocked, or the bridge is not running |
| Integration sets up but entities are unavailable | TCP 60128 blocked, or the serial port failed to open (`journalctl -u onkyo-bridge`) |
| `Cannot open /dev/ttyUSB0 (EBUSY)` | Another process holds the port; stop it, or stop the bridge to use manual `printf` |
| `Permission denied` opening the port | Service user not in `dialout` |
| Commands appear to do nothing | Write-only path: run the bidirectional check above. If there is no reply, it is a cable/port problem, not a bridge problem |
| Receiver does not respond in standby | Many Onkyo models keep RS-232 alive in standby; if yours does not, enable the equivalent of "Network Standby" or use a 12 V trigger |

## Uninstall

`deploy/uninstall.sh` reverts exactly what `install.sh` does: stops and
disables the service, removes the unit (and any stale `*.wants/` symlink),
`/opt/onkyo-bridge`, `/etc/default/onkyo-bridge`, and the `onkyo-bridge` system
user (which also clears its `dialout` membership).

```bash
cd <project>
sudo ./deploy/uninstall.sh              # full removal
sudo ./deploy/uninstall.sh --dry-run    # show what would be removed first
sudo ./deploy/uninstall.sh --keep-user  # keep the onkyo-bridge system user
sudo ./deploy/uninstall.sh --keep-config
```

It is safe to run when nothing is installed and safe to run twice, and it ends
with a verification section that reports anything left behind.

It does **not** touch `deploy/onkyo-bridge.user.service` installations (a
per-user unit under someone's home directory). If such a service is still
running, the script detects the leftover process and prints the command to stop
it:

```bash
systemctl --user disable --now onkyo-bridge
```

## License

MIT — see [LICENSE](LICENSE). Do what you like with it; just keep the copyright
notice and license text.

This covers the bridge, its installer scripts, and its tests. The `research/`
directory is scratch material read from upstream projects and is not part of the
distribution (it is git-ignored).
