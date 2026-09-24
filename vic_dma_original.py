from __future__ import annotations

from dataclasses import dataclass

from .logger import SystemLogger
from .vic_ii import VicII


@dataclass
class VicStealInfo:
    """Describes a VIC-II bus steal window."""
    steal: bool
    kind: str  # 'none' | 'badline' | 'sprite_ptr'
    col: int = -1           # cycle-within-window (useful for forensic traces)
    sprite: int = -1        # sprite index for sprite DMA windows
    raster: int = -1        # current raster (for convenience)


class VicDma:
    """VIC-II DMA / BA/AEC arbitration model (cycle-granular).

    This SID-focused emulator needs VIC bus arbitration to be *timing-correct* for real C64 code:
    VIC steals must stall the CPU mid-instruction (BA low / AEC off).

    Implemented:
      - Badline matrix fetch steals (40 cycles on badlines)
      - Sprite pointer DMA steal window (2 cycles per active sprite near end of line)

    Not implemented (yet):
      - Full VIC fetch pipeline (sprite data fetch, refresh, border logic, collisions, etc.)

    Even this partial model fixes the fundamental timing breakage compared to per-line budgets.
    """

    # Approximate: cycles 15..54 (40 cycles) are stolen on badlines (end-exclusive).
    BADLINE_START = 15
    BADLINE_LEN = 40
    BADLINE_END = BADLINE_START + BADLINE_LEN

    def __init__(self) -> None:
        self._last_logged_raster: int = -1

    @staticmethod
    def is_bad_line(vic: VicII) -> bool:
        # DEN is bit 4 of $D011. YSCROLL is bits 0..2 of $D011.
        ctrl1 = vic.read(0x11)
        raster = int(vic.rasterLine) & 0x1FF
        if (ctrl1 & 0x10) == 0:
            return False
        y_scroll = ctrl1 & 0x07
        if (raster & 0x07) != y_scroll:
            return False
        # Visible area approx: $30..$F7
        return (0x30 <= raster <= 0xF7)

    def _sprite_dma_active(self, vic: VicII, spr: int) -> bool:
        """Conservative sprite-DMA activation check (used mainly for CPU stalls + traces)."""
        try:
            enable = int(vic.regs[0x15]) & 0xFF  # $D015 sprite enable
            if not (enable & (1 << spr)):
                return False
            y_reg = 0x01 + (spr * 2)  # $D001, $D003, ...
            y0 = int(vic.regs[y_reg]) & 0xFF
            y = int(vic.rasterLine) & 0xFF
            # Nominal sprite height 21 lines. Ignore Y-expand for now.
            return y0 <= y <= ((y0 + 20) & 0xFF)
        except Exception:
            return False

    def steal_info(self, vic: VicII) -> VicStealInfo:
        raster = int(vic.rasterLine) & 0x1FF
        c = int(getattr(vic, 'cycleCounter', 0))

        bad = self.is_bad_line(vic)
        if bad and (self.BADLINE_START <= c < self.BADLINE_END):
            return VicStealInfo(steal=True, kind='badline', col=c - self.BADLINE_START, sprite=-1, raster=raster)

        # Sprite pointer DMA window: 16 cycles at end of rasterline (2 per sprite).
        ptr_start = max(0, int(vic.cyclesPerLine) - 16)
        if ptr_start <= c < (ptr_start + 16):
            slot = c - ptr_start
            spr = slot // 2
            if 0 <= spr <= 7 and self._sprite_dma_active(vic, spr):
                return VicStealInfo(steal=True, kind='sprite_ptr', col=slot, sprite=spr, raster=raster)

        return VicStealInfo(steal=False, kind='none', col=-1, sprite=-1, raster=raster)

    def cpu_has_bus(self, vic: VicII) -> bool:
        info = self.steal_info(vic)
        # Log badline entry once per raster for quick human debugging.
        if info.kind == 'badline' and self._last_logged_raster != int(vic.rasterLine):
            self._last_logged_raster = int(vic.rasterLine)
            SystemLogger.log(
                'VIC-II',
                f'Badline active: BA/AEC steals cycles {self.BADLINE_START}..{self.BADLINE_END - 1} (raster {vic.rasterLine})',
                'debug',
                category='vicdma',
                fields={'raster': int(vic.rasterLine), 'start': self.BADLINE_START, 'end': self.BADLINE_END - 1},
            )
        return not info.steal
