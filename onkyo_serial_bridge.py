#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bridge an Onkyo RS-232 (ISCP) serial port to eISCP over TCP/UDP 60128.

This makes an RS-232-only Onkyo/Integra receiver look like a native network
receiver, so Home Assistant's built-in ``onkyo`` integration can drive it
unmodified.

Why the translations below are needed
-------------------------------------
eISCP is ISCP with a 16-byte ``ISCP`` header prepended, but the framing is not
byte-identical, and getting this wrong makes Home Assistant drop the
connection:

* Instructions (HA -> receiver): payload is ``!1<code><param>`` + ``\\r\\n``.
  Over serial the receiver wants the same message terminated by a bare ``\\r``.
* Status (receiver -> HA): the receiver may terminate with ``\\x1a``, ``\\r``,
  ``\\n`` or a combination. ``aioonkyo`` only accepts ``\\x1a\\r\\n`` or
  ``\\r\\n`` on the TCP side, so terminators are normalised to ``\\x1a\\r\\n``.
* Discovery (HA -> receiver, UDP): answered by this bridge directly, because
  the reply format is different again -- ``\\x19\\r\\n`` terminated and
  ``model/port/area/identifier`` -- and because a receiver that is in standby
  may not answer at all.
* ``ECN`` status lines coming back from the serial port are swallowed. Their
  serial format has no ``identifier`` field, so forwarding them would raise a
  parse error in aioonkyo and tear the connection down. They are used only to
  learn the real model name.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import logging
import os
import re
import signal
import struct
import sys
import termios
from typing import Final

_LOGGER = logging.getLogger("onkyo-bridge")

# --- eISCP wire format -------------------------------------------------------

EISCP_MAGIC: Final = b"ISCP"
EISCP_HEADER: Final = struct.Struct("! 4s I I B 3s")
EISCP_HEADER_SIZE: Final = 16
DEFAULT_PORT: Final = 60128

# Terminators, by direction. See module docstring.
STATUS_END: Final = b"\x1a\r\n"
DISCOVERY_END: Final = b"\x19\r\n"
INSTRUCTION_END: Final = b"\r"

# Bytes that may terminate a serial line.
_TERMINATORS: Final = (0x1A, 0x0D, 0x0A)
_MAX_BUFFER: Final = 4096

# Serial reply to a discovery query: !1ECN<model>/<port>/<area>[/<identifier>]
_ECN_RE: Final = re.compile(
    rb"^!1ECN(?P<model>[^/]{1,64})/(?P<port>\d{5})/(?P<area>\w{2})(?:/(?P<ident>.{0,12}))?$"
)

_BAUD_RATES: Final = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)


def build_packet(payload: bytes) -> bytes:
    """Wrap an ISCP payload in an eISCP header."""
    return EISCP_HEADER.pack(EISCP_MAGIC, EISCP_HEADER_SIZE, len(payload), 1, b"\x00\x00\x00") + payload


def parse_header(header: bytes) -> tuple[int, int]:
    """Return (header_size, data_size) from an eISCP header."""
    magic, header_size, data_size, _version, _reserved = EISCP_HEADER.unpack(header)
    if magic != EISCP_MAGIC:
        raise ValueError(f"bad eISCP magic {magic!r}")
    if header_size < EISCP_HEADER_SIZE:
        raise ValueError(f"bad eISCP header size {header_size}")
    return header_size, data_size


def strip_terminators(payload: bytes) -> bytes:
    """Remove trailing CR/LF/SUB bytes from an ISCP payload."""
    return payload.rstrip(b"\x1a\r\n")


def extract_messages(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split a serial byte stream into complete ISCP messages.

    Returns the complete messages plus any unterminated remainder, which the
    caller should feed back in on the next read.
    """
    messages: list[bytes] = []
    index = 0

    while True:
        start = buffer.find(b"!", index)
        if start == -1:
            # Nothing that could begin a message. Keep nothing.
            return messages, b""

        cursor = start + 1
        end = -1
        while cursor < len(buffer):
            if buffer[cursor] in _TERMINATORS:
                end = cursor
                break
            cursor += 1

        if end == -1:
            # Incomplete message: keep it for the next read, unless it has
            # grown implausibly long (then assume we lost framing).
            remainder = buffer[start:]
            if len(remainder) > _MAX_BUFFER:
                _LOGGER.warning("Dropping oversized serial buffer (%d bytes)", len(remainder))
                return messages, b""
            return messages, remainder

        messages.append(buffer[start:end])

        cursor = end
        while cursor < len(buffer) and buffer[cursor] in _TERMINATORS:
            cursor += 1
        index = cursor
        if index >= len(buffer):
            return messages, b""


def default_identifier() -> str:
    """Mimic a real receiver: a 12-character hex identifier (usually the MAC)."""
    for name in sorted(os.listdir("/sys/class/net")):
        if name == "lo":
            continue
        try:
            with open(f"/sys/class/net/{name}/address", encoding="ascii") as handle:
                mac = handle.read().strip()
        except OSError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            return mac.replace(":", "").upper()
    return "000000000001"


class SerialLink:
    """A raw 9600 8N1 serial link to the receiver, held open."""

    def __init__(self, device: str, baud: int) -> None:
        self.device = device
        self.baud = baud
        self.fd: int | None = None

    def open(self) -> None:
        """Open and configure the port. Raises OSError on failure."""
        fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

        try:
            attrs = termios.tcgetattr(fd)

            # Equivalent to:
            #   stty 9600 cs8 -cstopb -parenb -crtscts -ixon -ixoff clocal raw -echo
            attrs[0] = 0  # iflag: no parity/break/software-flow translation
            attrs[1] = 0  # oflag: raw output
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag, 8N1, no HW flow
            attrs[3] = 0  # lflag: raw, no echo, no canonical processing
            attrs[4] = getattr(termios, f"B{self.baud}")  # ispeed
            attrs[5] = getattr(termios, f"B{self.baud}")  # ospeed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)

            # Serial lines are not exclusive by default; take exclusive access so
            # a stray `printf > /dev/ttyUSB0` cannot interleave with the bridge.
            with contextlib.suppress(OSError):
                fcntl.ioctl(fd, termios.TIOCEXCL, 0)

            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            raise

        self.fd = fd
        _LOGGER.info("Opened %s at %d baud (raw 8N1, no flow control)", self.device, self.baud)

    def close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None

    def write(self, data: bytes) -> None:
        if self.fd is None:
            raise OSError("serial port is not open")
        os.write(self.fd, data)


class Bridge:
    """Serial <-> eISCP bridge."""

    def __init__(
        self,
        device: str,
        baud: int,
        bind: str,
        port: int,
        model: str,
        identifier: str,
        learn_model: bool,
    ) -> None:
        self.serial = SerialLink(device, baud)
        self.bind = bind
        self.port = port
        self.model = model
        self.identifier = identifier[:12]
        self.learn_model = learn_model

        self.clients: set[asyncio.StreamWriter] = set()
        self.buffer = b""
        self._reopen_task: asyncio.Task[None] | None = None
        self._runner: asyncio.AbstractServer | None = None

    # --- serial -------------------------------------------------------------

    def _on_serial_readable(self) -> None:
        assert self.serial.fd is not None
        try:
            data = os.read(self.serial.fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            _LOGGER.warning("Serial read failed (%s); reopening", exc)
            self._close_serial()
            return

        if not data:
            return

        _LOGGER.debug("serial << %r", data)
        self.buffer += data
        messages, self.buffer = extract_messages(self.buffer)
        for message in messages:
            self._handle_serial_message(message)

    def _handle_serial_message(self, message: bytes) -> None:
        """Handle one complete ISCP message from the receiver."""
        # Swallow the discovery reply on the TCP path: its serial format has no
        # identifier field and uses a different terminator, so forwarding it
        # would make aioonkyo raise and drop the connection. Use it to learn the
        # model name instead.
        if message.startswith(b"!1ECN") or message.startswith(b"!xECN") or message.startswith(b"!pECN"):
            match = _ECN_RE.match(message)
            if match:
                model = match.group("model").decode("ascii", "replace")
                if model and model != self.model:
                    _LOGGER.info("Learned receiver model: %s", model)
                    self.model = model
            else:
                _LOGGER.debug("Ignoring unparsable ECN reply: %r", message)
            return

        payload = message + STATUS_END
        self._broadcast(build_packet(payload))

    def _broadcast(self, data: bytes) -> None:
        for writer in list(self.clients):
            if writer.is_closing():
                self.clients.discard(writer)
                continue
            try:
                writer.write(data)
            except Exception:  # noqa: BLE001 - a dead client must not stop the rest
                _LOGGER.debug("Dropping client after write failure")
                self.clients.discard(writer)

    def _close_serial(self) -> None:
        if self.serial.fd is not None:
            loop = asyncio.get_running_loop()
            with contextlib.suppress(Exception):
                loop.remove_reader(self.serial.fd)
        self.serial.close()
        self.buffer = b""
        self._schedule_reopen()

    def _schedule_reopen(self) -> None:
        if self._reopen_task is None or self._reopen_task.done():
            self._reopen_task = asyncio.create_task(self._reopen_loop())

    async def _reopen_loop(self) -> None:
        while self.serial.fd is None:
            await asyncio.sleep(3)
            try:
                self.serial.open()
            except OSError as exc:
                _LOGGER.warning("Cannot open %s (%s); retrying", self.serial.device, exc)
                continue

            loop = asyncio.get_running_loop()
            assert self.serial.fd is not None
            loop.add_reader(self.serial.fd, self._on_serial_readable)
            if self.learn_model:
                self._query_model()
            return

    def _query_model(self) -> None:
        with contextlib.suppress(OSError):
            self.serial.write(b"!1ECNQSTN\r")

    def _write_serial(self, core: bytes) -> None:
        if self.serial.fd is None:
            raise OSError("serial port is not open")
        _LOGGER.debug("serial >> %r", core + INSTRUCTION_END)
        self.serial.write(core + INSTRUCTION_END)

    # --- TCP: eISCP clients -------------------------------------------------

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.info("eISCP client connected: %s", peer)
        self.clients.add(writer)

        if self.learn_model and not self.model:
            self._query_model()

        try:
            while True:
                header = await reader.readexactly(EISCP_HEADER_SIZE)
                header_size, data_size = parse_header(header)
                if header_size > EISCP_HEADER_SIZE:
                    await reader.readexactly(header_size - EISCP_HEADER_SIZE)
                payload = await reader.readexactly(data_size)
                _LOGGER.debug("client  >> %r", payload)

                core = strip_terminators(payload)
                if not core:
                    continue
                try:
                    self._write_serial(core)
                except OSError as exc:
                    _LOGGER.warning("Cannot write to receiver (%s)", exc)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except ValueError as exc:
            _LOGGER.warning("Malformed eISCP stream from %s: %s", peer, exc)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Client handler failed for %s", peer)
        finally:
            self.clients.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()
            _LOGGER.info("eISCP client disconnected: %s", peer)

    # --- UDP: discovery ----------------------------------------------------

    def discovery_reply(self) -> bytes:
        """Build the discovery response a real network receiver would send."""
        model = (self.model or "TX-RZ800").encode("ascii", "replace")
        identifier = (self.identifier or "000000000001").encode("ascii", "replace")
        payload = b"!1ECN" + model + b"/" + str(self.port).encode() + b"/DX/" + identifier + DISCOVERY_END
        return build_packet(payload)

    class _DiscoveryProtocol(asyncio.DatagramProtocol):
        def __init__(self, bridge: "Bridge") -> None:
            self.bridge = bridge
            self.transport: asyncio.DatagramTransport | None = None

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            self.transport = transport  # type: ignore[assignment]

        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            # Accept any discovery query (Onkyo '!xECNQSTN' or Pioneer '!pECNQSTN').
            if b"ECNQSTN" not in data:
                _LOGGER.debug("Ignoring non-discovery datagram from %s: %r", addr, data)
                return
            _LOGGER.info("Discovery query from %s; replying as %s", addr, self.bridge.model)
            if self.transport is not None:
                self.transport.sendto(self.bridge.discovery_reply(), addr)
            # Refresh the model name in the background, if the receiver answers.
            self.bridge._query_model()

    # --- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        try:
            self.serial.open()
        except OSError as exc:
            _LOGGER.warning("Cannot open %s yet (%s); will keep retrying", self.serial.device, exc)
            self._schedule_reopen()
        else:
            assert self.serial.fd is not None
            loop.add_reader(self.serial.fd, self._on_serial_readable)
            if self.learn_model:
                self._query_model()

        self._runner = await asyncio.start_server(self.handle_client, self.bind, self.port)
        _LOGGER.info("eISCP TCP server listening on %s:%d", self.bind, self.port)

        transport, _ = await loop.create_datagram_endpoint(
            lambda: Bridge._DiscoveryProtocol(self),
            local_addr=(self.bind, self.port),
        )
        _LOGGER.info("eISCP discovery responder listening on %s:%d/udp", self.bind, self.port)
        _LOGGER.info("Advertising model=%s identifier=%s", self.model, self.identifier)

        stop = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))

        try:
            await stop
        finally:
            _LOGGER.info("Shutting down")
            transport.close()
            self._runner.close()
            await self._runner.wait_closed()
            for writer in list(self.clients):
                with contextlib.suppress(Exception):
                    writer.close()
            self._close_serial()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bridge an Onkyo RS-232 serial port to eISCP (TCP/UDP 60128).",
    )
    parser.add_argument(
        "-d",
        "--device",
        default="/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_*-if00-port0",
        help="Serial device. Prefer a /dev/serial/by-id/... path, which survives replugs.",
    )
    parser.add_argument("-b", "--baud", type=int, default=9600, choices=_BAUD_RATES)
    parser.add_argument("--bind", default="0.0.0.0", help="Address to bind TCP/UDP on.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="eISCP port (default 60128).")
    parser.add_argument("--model", default="TX-RZ800", help="Model name advertised during discovery.")
    parser.add_argument(
        "--identifier",
        default=None,
        help="Up to 12 characters, advertised during discovery. Defaults to this host's MAC.",
    )
    parser.add_argument(
        "--no-learn-model",
        action="store_true",
        help="Do not query the receiver for its model name.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def resolve_device(pattern: str) -> str:
    """Expand a glob device path, e.g. /dev/serial/by-id/usb-FTDI_*-if00-port0."""
    import glob

    if any(char in pattern for char in "*?["):
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise SystemExit(
                f"No serial device matches {pattern!r}.\n"
                "Check the adapter is plugged in (`ls -l /dev/serial/by-id/`) "
                "or pass --device explicitly."
            )
        if len(matches) > 1:
            _LOGGER.warning("Multiple devices match %s; using %s", pattern, matches[0])
        return matches[0]
    return pattern


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    device = resolve_device(args.device)
    bridge = Bridge(
        device=device,
        baud=args.baud,
        bind=args.bind,
        port=args.port,
        model=args.model,
        identifier=args.identifier or default_identifier(),
        learn_model=not args.no_learn_model,
    )

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
