from __future__ import annotations

"""Forensic trace dump structures.

This module defines a compact, serialization-friendly representation of:
 - per-cycle bus bytes (bit-cycle aligned by construction)
 - throttled frame telemetry (50/60 Hz)

The actual emulator is not a full phi2-accurate C64, but the *trace stream*
is cycle-aligned to the CPU cycles the core reports. We achieve this by:
 - recording each memory read/write as one bus cycle
 - inserting explicit IDLE cycles to match the instruction cycle count

This mirrors the intent of the TypeScript SidExporter used for forensic export.
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SidDumpMetadata:
    title: str
    author: str
    released: str
    clockFreq: int
    isNtsc: bool
    sidCount: int
    sidModels: List[str]


@dataclass
class CpuTelemetry:
    pc: int
    a: int
    x: int
    y: int


@dataclass
class VoiceTelemetry:
    acc: int
    lfsr: int
    env: int
    st: int
    w: int
    f: float


@dataclass
class SidChipTelemetry:
    registers: bytes
    voices: List[VoiceTelemetry]


@dataclass
class FrameTelemetry:
    cycles: int
    time: float
    cpu: CpuTelemetry
    chips: List[SidChipTelemetry]


@dataclass
class OptimizedWrites:
    """Per-cycle bus capture.

    cycles_u32le: bytes
        Little-endian uint32 per cycle index.
    data_u8: bytes
        One byte per cycle, representing the observed bus byte.
    """

    cycles_u32le: bytes
    data_u8: bytes


@dataclass
class SidDump:
    metadata: SidDumpMetadata
    optimizedWrites: OptimizedWrites
    ramInitial_b64: str
    ramFinal_b64: str
    originalSource_b64: str
    frames: List[FrameTelemetry]
