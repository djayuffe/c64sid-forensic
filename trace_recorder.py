from __future__ import annotations

import base64
import struct
from dataclasses import dataclass


@dataclass
class TraceBuffers:
    """Raw trace buffers.

    cycles_u32le: packed little-endian uint32 values, one per recorded cycle.
    data_u8: one byte per recorded cycle, representing the observed bus byte.
    """

    cycles_u32le: bytearray
    data_u8: bytearray


class SidTraceRecorder:
    """Per-cycle bus recorder.

    The CPU core provides instruction cycle counts but isn't micro-op / phi2
    accurate. We approximate a cycle-aligned bus stream by:

    - treating every memory read/write helper call as one bus cycle
    - padding the remainder of the instruction cycles with IDLE cycles

    This guarantees *cycle count* alignment with the CPU's reported cycles,
    which is enough for deterministic forensic diffing between runs.
    """

    def __init__(self) -> None:
        self._cycle: int = 0
        self._buf = TraceBuffers(bytearray(), bytearray())

    def set_cycle(self, cycle: int) -> None:
        """Force the current cycle index.

        Normally traces start at 0. If you enable tracing mid-run you can
        align the cycle index to the system's cycle counter.
        """
        self._cycle = max(0, int(cycle))

    @property
    def cycle(self) -> int:
        return self._cycle

    def tick(self, bus_byte: int) -> None:
        # cycles
        self._buf.cycles_u32le += struct.pack('<I', self._cycle & 0xFFFFFFFF)
        # data
        self._buf.data_u8.append(bus_byte & 0xFF)
        self._cycle += 1

    def tick_idle(self, n: int, bus_byte: int) -> None:
        b = bus_byte & 0xFF
        for _ in range(max(0, int(n))):
            self.tick(b)

    def snapshot(self) -> tuple[bytes, bytes]:
        return bytes(self._buf.cycles_u32le), bytes(self._buf.data_u8)


def b64encode_bytes(data: bytes, chunk_size: int = 256 * 1024) -> str:
    """Chunked base64 encoding to avoid huge intermediate strings."""
    if not data:
        return ""
    out = []
    for i in range(0, len(data), chunk_size):
        out.append(base64.b64encode(data[i:i + chunk_size]).decode('ascii'))
    return ''.join(out)
