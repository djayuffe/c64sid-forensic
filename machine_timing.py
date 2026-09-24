from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MachineModel:
    name: str  # 'PAL' | 'NTSC'
    isNtsc: bool
    clockHz: int
    cyclesPerLine: int
    linesPerFrame: int
    cyclesPerFrame: int


class MachineTiming:
    PAL = MachineModel(
        name='PAL',
        isNtsc=False,
        clockHz=985_248,
        cyclesPerLine=63,
        linesPerFrame=312,
        cyclesPerFrame=63 * 312,
    )

    NTSC = MachineModel(
        name='NTSC',
        isNtsc=True,
        clockHz=1_022_727,
        cyclesPerLine=65,
        linesPerFrame=263,
        cyclesPerFrame=65 * 263,
    )

    @staticmethod
    def get(isNtsc: bool) -> MachineModel:
        return MachineTiming.NTSC if isNtsc else MachineTiming.PAL
