from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

SidModel = Literal['6581', '8580', 'UNKNOWN']
PlaybackMethod = Literal['VBI_Call', 'CIA_Interrupt']
InterruptType = Literal['IRQ', 'NMI']


@dataclass
class SidHeader:
    magic: Literal['PSID', 'RSID']
    version: int
    dataOffset: int

    loadAddress: int
    initAddress: int
    playAddress: int

    songs: int
    startSong: int
    speed: int

    title: str
    author: str
    released: str

    flags: int
    isNtsc: bool
    clockFreq: int

    model: SidModel
    sidCount: int
    sidModels: List[SidModel]
    sidAddresses: List[int]

    relocStartPage: int = 0
    relocPageLength: int = 0

    c64BasicFlag: bool = False


@dataclass
class C64Roms:
    kernal: Optional[bytes] = None
    basic: Optional[bytes] = None
    chargen: Optional[bytes] = None

# Alias for compatibility
RomPack = C64Roms


@dataclass
class C64Config:
    enableHle: bool = True
    # RSID expects real ROM behavior. If ROMs are missing and this is False,
    # RSID playback will fail fast with a helpful error instead of silently
    # running with stub ROMs.
    allowHleForRsid: bool = False
    roms: C64Roms = field(default_factory=C64Roms)
    busPersistenceCycles: int = 0x1D00
    enableAdsrPipeline: bool = True
    # If True, SID advances per CPU phi2 cycle (phase wrap/sync/noise become cycle-accurate).
    # This is heavier than bulk stepping but is required for logic-analyzer-grade traces,
    # especially for hard-sync and noise clocking.
    sidCycleExact: bool = True
    # Optional board-model parameters for D418 digi (volume DAC) analog path.
    # If left as None, SidChip selects model-appropriate defaults.
    sidD418Rohm: Optional[float] = None
    sidD418CapLP_F: Optional[float] = None
    sidD418CapHP_F: Optional[float] = None
    sidD418Gain: Optional[float] = None

    # SID filter shaping overrides (optional).
    sidFilterCutoffScale: Optional[float] = None
    sidFilterCutoffOffsetHz: Optional[float] = None

    # Optional SID internal trace controls (JSON-friendly via SystemLogger categories).
    # 0 disables per-cycle snapshots. If >0, emit a snapshot every N SID cycles.
    sidTraceEveryCycles: int = 0
    # Voices to include in SID snapshots (0..2).
    sidTraceVoices: List[int] = field(default_factory=lambda: [0, 1, 2])
    # Include waveform/phase detail in snapshots.
    sidTraceWave: bool = True
    # Include envelope counters/raters in snapshots.
    sidTraceEnv: bool = True
    noiseSeed: int = 0x7FFFF8
    # If True, quantize selected DSP paths to fixed-point steps for cross-platform deterministic forensics.
    deterministicDSP: bool = False
    # Emulate the subtle A->D exponential-period stale-cycle glitch.
    enableAdsrADGlitch: bool = True
    exportDepth: Literal['NONE', 'BASIC', 'FULL'] = 'FULL'


@dataclass
class InterruptEvent:
    cycles: int
    type: InterruptType
    source: str
    vectorAddr: int
    handlerAddr: int