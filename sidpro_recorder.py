from __future__ import annotations

"""
SID-PRO Forensic (V6) capture helpers.

This module provides:

- CycleCounterTrace: a lightweight trace "sink" for Cpu6502 that only tracks
  the cycle index (it does not store per-cycle bus bytes). This keeps memory
  usage bounded even for long captures.

- SidProEventRecorder: records SID register writes as:
    * bus_cycles: float64 little-endian array (cycle indices)
    * bus_events: uint8 triplets [chip, reg, value]

The recorder is fed from Cpu6502's write observer hook (called for every CPU write).
"""

from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence
import struct


class CycleCounterTrace:
    """Lightweight trace compatible with Cpu6502's trace hooks.

    The Cpu6502 uses:
      - trace.tick(bus_byte) once per observed bus access
      - trace.tick_idle(n, last_bus) to pad to the instruction's total cycles

    We ignore the bus byte and only advance the cycle index.
    """

    def __init__(self) -> None:
        self._cycle: int = 0

    def set_cycle(self, cycle: int) -> None:
        self._cycle = max(0, int(cycle))

    @property
    def cycle(self) -> int:
        return self._cycle

    def tick(self, _bus_byte: int) -> None:
        self._cycle += 1

    def tick_idle(self, n: int, _bus_byte: int) -> None:
        self._cycle += max(0, int(n))


@dataclass
class SidProBuffers:
    """Raw SID-PRO V6 event buffers (uncompressed)."""
    cycles_f64le: bytearray
    events_u8: bytearray
    count: int = 0


class SidProEventRecorder:
    """Collect SID register write events for SID-PRO V6 export."""

    def __init__(self, sid_bases: Sequence[int]):
        self.sid_bases = [int(b) & 0xFFFF for b in sid_bases]
        self._buf = SidProBuffers(bytearray(), bytearray(), 0)

    def observe_write(self, cycle: int, addr: int, value: int) -> None:
        """Observe a CPU write at given bus-cycle index."""
        a = int(addr) & 0xFFFF
        v = int(value) & 0xFF
        c = int(cycle)

        # Match against each SID base. Only record $00..$1C (0..28) offsets.
        for chip, base in enumerate(self.sid_bases):
            if base <= a <= base + 0x1C:
                reg = a - base
                # bus_cycles: float64le
                self._buf.cycles_f64le += struct.pack('<d', float(c))
                # bus_events: triplet
                self._buf.events_u8 += bytes((chip & 0xFF, reg & 0xFF, v))
                self._buf.count += 1
                return  # One SID range match at most

    def snapshot(self) -> tuple[bytes, bytes, int]:
        """Return (cycles_f64le_bytes, events_triplets_bytes, count)."""
        return bytes(self._buf.cycles_f64le), bytes(self._buf.events_u8), int(self._buf.count)
