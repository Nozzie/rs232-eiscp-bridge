#!/usr/bin/env python3
"""Verify a running bridge from a remote machine, using the same aioonkyo
library Home Assistant uses.

This performs READ-ONLY queries: it never changes the receiver's state.

    .venv/bin/python tests/remote_check.py 192.168.12.170
"""

from __future__ import annotations

import asyncio
import sys

import aioonkyo
from aioonkyo import Zone, query


async def main(host: str, port: int = 60128) -> int:
    print(f"1. Discovery + interview against {host}:{port}")
    info = await asyncio.wait_for(aioonkyo.interview(host, port=port), timeout=6)
    print(f"   model={info.model_name!r} port={info.port} identifier={info.identifier!r}")
    print("   -> HA will accept this receiver\n")

    print("2. Connect and query (read-only), as HA does on setup")
    seen: list[str] = []
    async with aioonkyo.connect(info) as receiver:
        # HA probes each zone with a power query to learn which exist.
        for zone in (Zone.MAIN, Zone.ZONE2, Zone.ZONE3):
            await receiver.write(query.Power(zone))
        for zone in (Zone.MAIN,):
            await receiver.write(query.Volume(zone))
            await receiver.write(query.InputSource(zone))

        while True:
            try:
                message = await asyncio.wait_for(receiver.read(), timeout=1.5)
            except asyncio.TimeoutError:
                break
            if message is None:
                break
            seen.append(f"{type(message).__name__}: {message}")
            print(f"   << {message}")

    print(f"\n3. Result: {len(seen)} status message(s) received")
    if not seen:
        print("   FAILED - the bridge is listening but produced no status.")
        return 1
    print("   OK - discovery, TCP control and status all work end to end.")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "192.168.12.170"
    sys.exit(asyncio.run(main(target)))
