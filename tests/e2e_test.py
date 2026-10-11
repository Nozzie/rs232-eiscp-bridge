#!/usr/bin/env python3
"""End-to-end test for onkyo_serial_bridge using a pseudo-terminal as the receiver.

Run with the venv that has aioonkyo installed:

    .venv/bin/python tests/e2e_test.py

A PTY stands in for the AVR's serial port, and the real ``aioonkyo`` library
(the one Home Assistant uses) drives the bridge exactly as HA would.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aioonkyo
from aioonkyo import Zone, query, status
from onkyo_serial_bridge import EISCP_HEADER, EISCP_HEADER_SIZE, Bridge, build_packet

TEST_PORT = 16028  # non-privileged, unlikely to clash

FAKE_MODEL = "TX-RZ800"
# Deliberately the 3-field form a real receiver sends over serial: no identifier.
FAKE_ECN_REPLY = f"!1ECN{FAKE_MODEL}/{TEST_PORT}/DX".encode()

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


class FakeReceiver:
    """Plays an AVR on the master side of a PTY."""

    def __init__(self, master_fd: int) -> None:
        self.master_fd = master_fd
        self.received: list[bytes] = []
        self._buffer = b""

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master_fd, 4096)
        except BlockingIOError:
            return
        if not data:
            return
        self._buffer += data
        # Commands arrive CR-terminated.
        while b"\r" in self._buffer:
            line, self._buffer = self._buffer.split(b"\r", 1)
            line = line.strip(b"\x1a\n")
            if not line:
                continue
            self.received.append(line)
            self._respond(line)

    def _respond(self, line: bytes) -> None:
        if line == b"!1ECNQSTN":
            self.send(FAKE_ECN_REPLY)
        elif line.endswith(b"QSTN") and line.startswith(b"!1PWR"):
            # Power query -> report "on".
            self.send(b"!1PWR01")
        elif line.startswith(b"!1PWR"):
            # Echo, as real ISCP receivers do.
            self.send(line)

    def send(self, message: bytes) -> None:
        """Emit a status line, terminated the way real hardware does."""
        os.write(self.master_fd, message + b"\x1a\r\n")

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.add_reader(self.master_fd, self._on_readable)


async def read_packet(reader: asyncio.StreamReader) -> bytes:
    """Read one eISCP packet from a TCP stream and return its payload."""
    header = await reader.readexactly(EISCP_HEADER_SIZE)
    _magic, header_size, data_size, _version, _reserved = EISCP_HEADER.unpack(header)
    if header_size > EISCP_HEADER_SIZE:
        await reader.readexactly(header_size - EISCP_HEADER_SIZE)
    return await reader.readexactly(data_size)


async def main() -> int:
    loop = asyncio.get_running_loop()

    master_fd, slave_fd = os.openpty()
    device = os.ttyname(slave_fd)
    os.set_blocking(master_fd, False)
    os.close(slave_fd)  # the bridge opens the slave itself
    print(f"PTY receiver on {device}\n")

    fake = FakeReceiver(master_fd)
    fake.start(loop)

    bridge = Bridge(
        device=device,
        baud=9600,
        bind="127.0.0.1",
        port=TEST_PORT,
        model="TX-RZ800",
        identifier="AABBCCDDEEFF",
        learn_model=True,
    )
    bridge_task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.6)  # let the bridge open the port and settle

    try:
        # --- 1. Discovery / interview, exactly as HA's config flow does --------
        print("1. Discovery + interview via aioonkyo")
        try:
            info = await asyncio.wait_for(aioonkyo.interview("127.0.0.1", port=TEST_PORT), timeout=5)
            check("interview() returned ReceiverInfo", True, str(info))
            check("model name learned from receiver", info.model_name == FAKE_MODEL, info.model_name)
            check("advertised iscp_port is correct", info.port == TEST_PORT, str(info.port))
            # aioonkyo requires a non-empty identifier (1-12 chars).
            check("identifier is 1-12 chars", 1 <= len(info.identifier) <= 12, repr(info.identifier))
        except Exception as exc:  # noqa: BLE001
            check("interview() succeeded", False, f"{type(exc).__name__}: {exc}")
            info = None

        # --- 2. TCP control path + status path --------------------------------
        print("\n2. Control and status over eISCP (aioonkyo connect)")
        if info is not None:
            try:
                async with aioonkyo.connect(info) as receiver:
                    await receiver.write(query.Power(Zone.MAIN))
                    await asyncio.sleep(0.4)
                    check(
                        "command reached the serial port",
                        any(b"PWR" in line for line in fake.received),
                        f"receiver saw: {[l.decode('ascii', 'replace') for l in fake.received]}",
                    )
                    check("serial terminator is a bare CR", fake.received and fake.received[-1] == b"!1PWRQSTN", str(fake.received[-1:]))

                    got = await asyncio.wait_for(receiver.read(), timeout=3)
                    check("status parsed by aioonkyo", isinstance(got, status.Power), f"{type(got).__name__}: {got}")
            except Exception as exc:  # noqa: BLE001
                check("control/status round trip", False, f"{type(exc).__name__}: {exc}")

        # --- 3. ECN must not be forwarded to TCP clients ----------------------
        print("\n3. ECN filtering on the TCP path")
        control_reader, control_writer = await asyncio.open_connection("127.0.0.1", TEST_PORT)
        try:
            await asyncio.sleep(0.2)

            fake.send(FAKE_ECN_REPLY)  # must be swallowed, not forwarded
            try:
                payload = await asyncio.wait_for(read_packet(control_reader), timeout=1.0)
                check("ECN not forwarded to TCP clients", False, f"unexpectedly got {payload!r}")
            except asyncio.TimeoutError:
                check("ECN not forwarded to TCP clients", True)

            # The connection must still be alive and forwarding normally.
            fake.send(b"!1PWR01")
            payload = await asyncio.wait_for(read_packet(control_reader), timeout=2)
            check(
                "status forwarded with \\x1a\\r\\n terminator",
                payload == b"!1PWR01\x1a\r\n",
                repr(payload),
            )
        finally:
            control_writer.close()
            with contextlib.suppress(Exception):
                await control_writer.wait_closed()

        # --- 4. Pioneer-style discovery query is answered too -----------------
        print("\n4. Pioneer discovery query")
        reply: list[bytes] = []

        class Collector(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                reply.append(data)

        transport, _ = await loop.create_datagram_endpoint(Collector, local_addr=("127.0.0.1", 0))
        transport.sendto(build_packet(b"!pECNQSTN\r\n"), ("127.0.0.1", TEST_PORT))
        await asyncio.sleep(0.5)
        transport.close()
        check("Pioneer query answered", len(reply) == 1, f"{len(reply)} reply/replies")
        if reply:
            body = reply[0][EISCP_HEADER_SIZE:]
            check("reply ends with \\x19\\r\\n", body.endswith(b"\x19\r\n"), repr(body[-4:]))

    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except asyncio.CancelledError:
            pass
        with contextlib.suppress(Exception):
            loop.remove_reader(master_fd)
        os.close(master_fd)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
