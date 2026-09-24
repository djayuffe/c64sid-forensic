from __future__ import annotations

from .logger import SystemLogger


def _u8(v: int) -> int:
    return v & 0xFF


class VicII:
    def __init__(self):
        self.rasterLine = 0
        self.cyclesPerLine = 63
        self.linesPerFrame = 312
        self.cycleCounter = 0
        self.reg = bytearray(0x40)
        self.irqEnabled = 0
        self.irqStatus = 0
        self.irqLine = False

    def reset(self) -> None:
        for i in range(len(self.reg)):
            self.reg[i] = 0
        self.rasterLine = 0
        self.cycleCounter = 0
        self.reg[0x11] = 0x1B
        self.irqLine = False
        self.irqStatus = 0
        self.irqEnabled = 0
        SystemLogger.log('VIC-II', 'Hardware Reset. Initializing PAL state.', 'debug')

    def set_standard(self, ntsc: bool) -> None:
        self.linesPerFrame = 263 if ntsc else 312
        self.cyclesPerLine = 65 if ntsc else 63
        SystemLogger.log('VIC-II', f"Video standard set to {'NTSC' if ntsc else 'PAL'}.", 'debug')

    def read(self, addr: int) -> int:
        r = addr & 0x3F
        if r == 0x11:
            return (self.reg[0x11] & 0x7F) | ((self.rasterLine & 0x100) >> 1)
        if r == 0x12:
            return self.rasterLine & 0xFF
        if r == 0x19:
            return (self.irqStatus | 0x70) & 0xFF
        return self.reg[r]

    def write(self, addr: int, val: int) -> None:
        r = addr & 0x3F
        v = _u8(val)
        if r == 0x11:
            old = self.reg[0x11]
            self.reg[0x11] = v
            if (v & 0x10) != (old & 0x10):
                SystemLogger.log('VIC-II', f"Screen {'Enabled' if (v & 0x10) else 'Disabled (Blanking)'}", 'debug')
            if (v & 0x60) != (old & 0x60):
                bcm = (v & 0x40) != 0
                ecm = (v & 0x20) != 0
                SystemLogger.log('VIC-II', f'Display Mode Change: BCM={bcm}, ECM={ecm}', 'debug')
        elif r == 0x12:
            self.reg[0x12] = v
        elif r == 0x16:
            old = self.reg[0x16]
            self.reg[0x16] = v
            if (v & 0x10) != (old & 0x10):
                SystemLogger.log('VIC-II', f"Multi-Color Mode {'Activated' if (v & 0x10) else 'Deactivated'}", 'debug')
        elif r == 0x19:
            self.irqStatus &= ~(v & 0x0F)
            self._update_irq_line()
        elif r == 0x1A:
            self.irqEnabled = v & 0x0F
            SystemLogger.log('VIC-II', f'Interrupt Mask Change: %{self.irqEnabled:04b}', 'debug')
            self._update_irq_line()
        else:
            self.reg[r] = v

    def _update_irq_line(self) -> None:
        active = (self.irqStatus & self.irqEnabled & 0x0F) != 0
        if active and not self.irqLine:
            SystemLogger.log('VIC-II', f'Raster IRQ Latch High (Line {self.rasterLine})', 'debug')
        self.irqLine = bool(active)
        if active:
            self.irqStatus |= 0x80
        else:
            self.irqStatus &= ~0x80

    def step(self, cycles: int) -> None:
        self.cycleCounter += max(0, int(cycles))
        while self.cycleCounter >= self.cyclesPerLine:
            self.cycleCounter -= self.cyclesPerLine
            self.rasterLine += 1
            if self.rasterLine >= self.linesPerFrame:
                self.rasterLine = 0
            target_line = self.reg[0x12] | ((self.reg[0x11] & 0x80) << 1)
            if self.rasterLine == target_line:
                if (self.irqStatus & 0x01) == 0:
                    self.irqStatus |= 0x01
                    self._update_irq_line()
