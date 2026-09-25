from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .logger import SystemLogger
from .constants import MAX_CALL_CYCLES, IRQ_VECTOR, NMI_VECTOR
from .cpu6502 import Cpu6502
from .machine_timing import MachineTiming, MachineModel
from .memory_bank import MemoryBank
from .sid_chip import SidChip
from .sid_types import C64Config, InterruptEvent
from .vic_dma import VicDma


def _u16(v: int) -> int:
    return v & 0xFFFF


@dataclass
class C64SystemStats:
    cpuCycles: int = 0  # global PHI2 cycles (including VIC steals)
    instructions: int = 0


class C64System:
    """C64 system wrapper with cycle-granular bus arbitration.

    Key properties:
      - One bus transaction per PHI2 (CPU R/W or VIC DMA fetch)
      - VIC badline steals stall the CPU mid-instruction (BA/AEC)
      - Per-cycle stepping of VIC/CIA/SID for logic-analyzer-grade timing
    """

    def __init__(self, cfg: Optional[C64Config] = None):
        self.cfg = cfg or C64Config()
        self.memory = MemoryBank(self.cfg)
        self.model: MachineModel = MachineTiming.PAL

        self.sid: Optional[SidChip] = None
        self.sids: List[SidChip] = []

        self.stats = C64SystemStats()
        self._pending_irqs: List[InterruptEvent] = []

        # VIC DMA / BA-AEC arbitration
        self.vic_dma = VicDma()
        self._vic_irq_prev = False
        self._cia1_irq_prev = False
        self._cia2_irq_prev = False

        # Create CPU with bus-cycle wrappers (so every CPU read/write becomes one PHI2).
        self.cpu = Cpu6502(self._cpu_read, self._cpu_write)
        self.cpu.set_bus_arbitration(self._cpu_can_access_bus, self._vic_steal_cycle)

        SystemLogger.log('C64', 'C64System init (CPU+Memory+VIC+CIA ready)', 'debug', category='boot')

    def set_model(self, ntsc: bool) -> None:
        self.model = MachineTiming.get(ntsc)
        self.memory.vic.set_standard(ntsc)
        # CIA TOD/serial timing depends on phi2 clock.
        try:
            self.memory.cia1.set_clock_hz(self.model.clockHz)
            self.memory.cia2.set_clock_hz(self.model.clockHz)
        except Exception:
            pass
        SystemLogger.log('C64', f"Timing model: {'NTSC' if ntsc else 'PAL'} | clock={self.model.clockHz}Hz | cycles/frame={self.model.cyclesPerFrame}", 'debug', category='boot')

    def attach_sid(self, sid: SidChip, base: int = 0xD400) -> None:
        self.attach_sids([sid], [base])

    def attach_sids(self, sids: List[SidChip], bases: List[int]) -> None:
        self.sids = sids
        self.sid = sids[0] if sids else None
        self.memory.install_sids(sids, bases)
        SystemLogger.log('C64', f"attach_sids: count={len(sids)} bases={[hex(b) for b in bases]}", 'debug', category='boot')

    def load_program(self, addr: int, data: bytes) -> None:
        SystemLogger.log('C64', f"load_program: {len(data)} bytes @ ${addr:04X}", 'debug', category='boot')
        self.memory.load(addr, data)

    def reset(self) -> None:
        SystemLogger.log('C64', 'System reset (RAM+devices+CPU)', 'debug', category='boot')
        self.stats = C64SystemStats()
        self._pending_irqs.clear()
        self._vic_irq_prev = False
        self._cia1_irq_prev = False
        self._cia2_irq_prev = False

        self.memory.reset()
        self.cpu.reset()
        SystemLogger.log('C64', f"After reset: PC=${self.cpu.pc:04X} SP=${self.cpu.sp:02X}", 'debug', category='boot')

    # ------------------------------------------------------------------
    # Interrupt queueing + servicing (instruction boundary)
    # ------------------------------------------------------------------
    def queue_interrupt(self, evt: InterruptEvent) -> None:
        self._pending_irqs.append(evt)

    def _service_interrupts(self) -> None:
        # NMI has priority over IRQ - only one serviced per instruction boundary
        for evt in self._pending_irqs[:]:
            if evt.type == 'NMI':
                self._pending_irqs.remove(evt)
                SystemLogger.log('C64', f"NMI from {evt.source}", 'debug', category='boot')
                _ = self.cpu.nmi()
                return

        for evt in self._pending_irqs[:]:
            if evt.type == 'IRQ':
                self._pending_irqs.remove(evt)
                SystemLogger.log('C64', f"IRQ from {evt.source}", 'debug', category='boot')
                _ = self.cpu.irq()
                return

    # ------------------------------------------------------------------
    # Cycle-granular bus + device stepping
    # ------------------------------------------------------------------
    def _tick_one_phi2(self, owner: str, rw: str, addr: int, value: int) -> None:
        """Advance the whole machine by exactly one PHI2 cycle."""
        cycle = int(self.stats.cpuCycles)

        # Optional per-cycle bus event (very noisy; gated by category).
        if SystemLogger.category_enabled('bus'):
            SystemLogger.log(
                'BUS',
                f"cy={cycle} {owner} {rw} ${addr:04X} -> ${value & 0xFF:02X}",
                'debug',
                category='bus',
                fields={
                    'cycle': cycle,
                    'owner': owner,
                    'rw': rw,
                    'addr': {'dec': int(addr) & 0xFFFF, 'hex': f"0x{int(addr) & 0xFFFF:04X}"},
                    'value': {'dec': int(value) & 0xFF, 'hex': f"0x{int(value) & 0xFF:02X}"},
                },
            )

        # Global bus/open-bus decay modeled once per phi2.
        try:
            self.memory.decay_bus(1)
        except Exception:
            pass

        # Step SID(s), CIA(s), VIC by one phi2.
        if self.sid:
            for s in self.sids:
                s.update(1)
        self.memory.cia1.step(1)
        self.memory.cia2.step(1)
        self.memory.vic.step(1)

        # Convert VIC/CIA IRQ lines into queued events (edge detect).
        vic_irq = bool(getattr(self.memory.vic, 'irqLine', False))
        if vic_irq and not self._vic_irq_prev:
            self.queue_interrupt(InterruptEvent(
                cycles=cycle,
                type='IRQ',
                source='VIC',
                vectorAddr=IRQ_VECTOR,
                handlerAddr=0,
            ))
            # Keep legacy behavior: ack VIC raster IRQ bit 0 immediately (write 1 to clear).
            try:
                self.memory.vic.write(0xD019, 0x01)
            except Exception:
                pass
        self._vic_irq_prev = vic_irq

        cia1_irq = bool(getattr(self.memory.cia1, 'irqLine', False))
        if cia1_irq and not self._cia1_irq_prev:
            self.queue_interrupt(InterruptEvent(
                cycles=cycle,
                type='IRQ',
                source='CIA1',
                vectorAddr=IRQ_VECTOR,
                handlerAddr=0,
            ))
            try:
                _ = self.memory.cia1.read(0xDC0D)
            except Exception:
                pass
        self._cia1_irq_prev = cia1_irq

        cia2_irq = bool(getattr(self.memory.cia2, 'irqLine', False))
        if cia2_irq and not self._cia2_irq_prev:
            self.queue_interrupt(InterruptEvent(
                cycles=cycle,
                type='NMI',
                source='CIA2',
                vectorAddr=NMI_VECTOR,
                handlerAddr=0,
            ))
            try:
                _ = self.memory.cia2.read(0xDD0D)
            except Exception:
                pass
        self._cia2_irq_prev = cia2_irq

        # Commit the global cycle.
        self.stats.cpuCycles = cycle + 1

    def _cpu_read(self, addr: int) -> int:
        a = _u16(addr)
        v = int(self.memory.read(a)) & 0xFF
        self._tick_one_phi2('CPU', 'R', a, v)
        return v

    def _cpu_write(self, addr: int, val: int) -> None:
        a = _u16(addr)
        v = int(val) & 0xFF
        self.memory.write(a, v)
        self._tick_one_phi2('CPU', 'W', a, v)

    def _cpu_can_access_bus(self) -> bool:
        # If VIC is stealing the bus this cycle, CPU must stall.
        return self.vic_dma.cpu_has_bus(self.memory.vic)

    def _vic_steal_cycle(self) -> int:
        """Execute one VIC DMA cycle while the CPU has lost the bus."""
        vic = self.memory.vic
        info = self.vic_dma.steal_info(vic)
        if not info.steal:
            return 0

        raster = int(vic.rasterLine) & 0x1FF
        if info.kind == 'badline':
            col = int(info.col) % 40
            addr = 0x0400 + (((raster - 0x30) * 40 + col) % 1000)
        elif info.kind == 'sprite_ptr':
            addr = 0x07F8 + (int(info.sprite) & 0x07)
        else:  # sprite data: model the three bus reads without inventing RAM writes.
            addr = 0x3FFF

        # MemoryBank currently exposes a single-address read interface.  The
        # bus owner/reason remains represented by the system-level trace below.
        value = int(self.memory.read(addr)) & 0xFF
        self._tick_one_phi2('VIC', 'R', addr, value)
        return value

    # ------------------------------------------------------------------
    # Public execution
    # ------------------------------------------------------------------
    def step(self, max_cycles: int) -> int:
        """Run until ``max_cycles`` global PHI2 cycles have elapsed."""
        budget = max(0, int(max_cycles))
        start = int(self.stats.cpuCycles)
        target = start + budget
        while self.stats.cpuCycles < target:
            self._service_interrupts()
            if self.stats.cpuCycles >= target:
                break
            self.cpu.step()
            self.stats.instructions += 1
        return int(self.stats.cpuCycles) - start

    def call(self, addr: int, a: int = 0, x: int = 0, y: int = 0) -> None:
        """Run a host-invoked 6502 subroutine until its outer RTS returns."""
        self.cpu.a, self.cpu.x, self.cpu.y = a & 0xFF, x & 0xFF, y & 0xFF
        self.cpu.hle_jsr(addr & 0xFFFF)
        depth, safety, start = 1, MAX_CALL_CYCLES, int(self.stats.cpuCycles)
        while depth > 0 and int(self.stats.cpuCycles) - start < safety:
            opcode = self.memory.peek(self.cpu.pc)
            self.cpu.step()
            self.stats.instructions += 1
            if opcode == 0x20:
                depth += 1
            elif opcode == 0x60:
                depth -= 1
        if int(self.stats.cpuCycles) - start >= safety:
            SystemLogger.log('C64', f'call() safety stop at {MAX_CALL_CYCLES} cycles', 'warn')

    def run_for_frames(self, frames: int) -> None:
        for _ in range(max(0, int(frames))):
            self.step(self.model.cyclesPerFrame)
