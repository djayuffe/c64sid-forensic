from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .cpu_illegals import CpuIllegals
from .trace_recorder import SidTraceRecorder
from .logger import SystemLogger
from .sid_names import analyze_sid_write, get_sid_reg_name
from typing import Any, Protocol


class TraceLike(Protocol):
    @property
    def cycle(self) -> int: ...
    def tick(self, bus_byte: int) -> None: ...
    def tick_idle(self, n: int, bus_byte: int) -> None: ...



def _u8(v: int) -> int:
    return v & 0xFF


def _u16(v: int) -> int:
    return v & 0xFFFF


@dataclass
class CpuState:
    a: int
    x: int
    y: int
    sp: int
    pc: int
    flags: int
    cycles: int


class Cpu6502:
    # Flag bits
    C = 0x01
    Z = 0x02
    I = 0x04
    D = 0x08
    B = 0x10
    U = 0x20
    V = 0x40
    N = 0x80

    def __init__(self, read_fn: Callable[[int], int], write_fn: Callable[[int, int], None]):
        self.a = 0
        self.x = 0
        self.y = 0
        self.sp = 0xFF
        self.pc = 0
        self.flags = Cpu6502.U | Cpu6502.I
        self.cycles = 0
        self.read_fn = read_fn
        self.write_fn = write_fn

        # Optional per-cycle bus trace.
        self._trace: Optional[TraceLike] = None
        self._trace_accesses: int = 0
        self._trace_last_bus: int = 0xFF

        # Optional write observer (used for SID-PRO event capture)
        self._write_observer: Optional[Any] = None

        # Bus arbitration hooks (VIC BA/AEC stalls)
        self._bus_can_access: Optional[Any] = None  # Callable[[], bool]
        self._bus_steal_cycle: Optional[Any] = None  # Callable[[], int]
        self._stall_cycles: int = 0

    def set_trace(self, trace: Optional[TraceLike]) -> None:
        """Enable/disable per-cycle bus tracing."""
        self._trace = trace
        self._trace_accesses = 0
        self._trace_last_bus = 0xFF

    def set_write_observer(self, observer: Optional[Any]) -> None:
        """Set an observer called on every CPU write.

        Signature: observer(cycle_index: int, addr: int, value: int) -> None

        The cycle_index is taken from the active trace (bus-cycle aligned). If no trace
        is installed, cycle_index will be -1.
        """
        self._write_observer = observer


    def set_bus_arbitration(self, can_access_bus, steal_cycle) -> None:
        """Install bus arbitration callbacks.

        can_access_bus(): bool
            Returns True if the CPU may perform a bus access on this cycle.

        steal_cycle(): int
            Called when the CPU is stalled by VIC DMA. Must advance the *system*
            by exactly one PHI2 cycle and return the observed bus byte for tracing.
        """
        self._bus_can_access = can_access_bus
        self._bus_steal_cycle = steal_cycle

    def _stall_for_bus(self) -> None:
        """Stall the CPU until it owns the bus for the next access.

        This models VIC-II BA/AEC / RDY-like behavior: the CPU is halted mid-instruction
        and the instruction's cycle count stretches by the stolen cycles.
        """
        if self._bus_can_access is None or self._bus_steal_cycle is None:
            return
        # Busy-wait: each iteration consumes exactly one PHI2.
        while True:
            try:
                ok = bool(self._bus_can_access())
            except Exception:
                ok = True
            if ok:
                return
            try:
                bus_byte = int(self._bus_steal_cycle()) & 0xFF
            except Exception:
                bus_byte = self._trace_last_bus & 0xFF
            # Count stall cycles so instruction timing stretches correctly
            self._stall_cycles += 1
            # Record the bus byte for forensic trace determinism
            self._trace_tick(bus_byte)



    def _trace_begin(self) -> None:
        # Reset per-instruction accounting (even when no trace object is installed).
        self._trace_accesses = 0
        self._stall_cycles = 0

    def _trace_tick(self, bus_byte: int) -> None:
        # Always count accesses so we can synthesize missing bus states even when
        # tracing is disabled.
        self._trace_accesses += 1
        self._trace_last_bus = _u8(bus_byte)
        if self._trace is None:
            return
        self._trace.tick(self._trace_last_bus)

    def _trace_end(self, base_cycles: int) -> int:
        """Finalize per-instruction cycle accounting.

        We pad to the *effective* cycle count:
          effective = base_cycles + stall_cycles

        Instead of recording opaque 'idle' cycles, we synthesize real bus reads
        (opcode-prefetch style) so the system sees one bus transaction per PHI2.
        """
        total = int(base_cycles) + int(self._stall_cycles)
        # Pad with dummy bus reads until we have emitted `total` cycles for this instruction.
        idle = total - int(self._trace_accesses)
        while idle > 0:
            # Dummy/prefetch read from current PC (does not change PC).
            try:
                _ = self.read(self.pc)
            except Exception:
                # As a fallback, just advance the trace with the last bus byte.
                self._trace_tick(self._trace_last_bus)
            idle -= 1
        return total

    def snapshot(self) -> CpuState:
        return CpuState(self.a, self.x, self.y, self.sp, self.pc, self.flags, self.cycles)

    # --- HLE helpers ---
    def hle_jsr(self, target_addr: int) -> int:
        """High-level JSR helper used by the standalone player.

        The coordinator's C64System.call() is a host-side helper and does not
        execute a real JSR opcode stream. Without an explicit cycle accounting
        step, the stack writes performed to emulate JSR would otherwise:

        - be recorded by the forensic bus trace
        - but NOT be counted in CPU/system cycle counters

        That would break the "bit-cycle aligned" property of the exported
        forensic stream.

        We therefore model a JSR as a 6-cycle event:
          - push return address (PC-1) high/low
          - set PC to target
          - pad the remaining cycles as IDLE in the trace

        This does *not* claim to be phi2-accurate, but it preserves cycle count
        alignment with the rest of the core.
        """
        self._trace_begin()

        ret = _u16(self.pc - 1)
        self.push((ret >> 8) & 0xFF)
        self.push(ret & 0xFF)
        self.pc = _u16(target_addr)

        total = 6
        total = self._trace_end(total)
        self.cycles += total
        return total

    # --- Memory helpers ---
    def read(self, addr: int) -> int:
        a = _u16(addr)
        # VIC-II BA/AEC can stall the CPU mid-instruction.
        self._stall_for_bus()
        # Best-effort bus-cycle index for verbose logs
        cyc = -1
        if self._trace is not None:
            try:
                cyc = int(self._trace.cycle)
            except Exception:
                cyc = -1

        # Propagate CPU context into memory subsystem (if it supports it)
        mem = getattr(self.read_fn, '__self__', None)
        if mem is not None:
            try:
                setattr(mem, '_cpu_cycle', cyc)
                setattr(mem, '_cpu_pc', int(self.pc))
            except Exception:
                pass

        v = _u8(self.read_fn(a))

        if SystemLogger.category_enabled('memrw') or SystemLogger.category_enabled('memread'):
            region = None
            try:
                if mem is not None and hasattr(mem, 'classify_address'):
                    region = mem.classify_address(a)
            except Exception:
                region = None
            SystemLogger.log(
                'MEMR',
                f"cy={cyc} pc=${self.pc:04X} READ @{a:04X} -> ${v:02X}" ,
                'debug',
                category='memrw' if SystemLogger.category_enabled('memrw') else 'memread',
                fields={
                    'cycle': cyc,
                    'pc': int(self.pc),
                    'pc_hex': f"0x{int(self.pc) & 0xFFFF:04X}",
                    'addr': a,
                    'addr_hex': f"0x{int(a) & 0xFFFF:04X}",
                    'value': v,
                    'value_hex': f"0x{int(v) & 0xFF:02X}",
                    'region': (region.get('region') if isinstance(region, dict) else None),
                    'region_detail': region,
                },
            )

        self._trace_tick(v)
        return v

    def write(self, addr: int, val: int) -> None:
        v = _u8(val)
        a = _u16(addr)
        # VIC-II BA/AEC can stall the CPU mid-instruction.
        self._stall_for_bus()
        cyc = -1
        if self._trace is not None:
            try:
                cyc = int(self._trace.cycle)
            except Exception:
                cyc = -1

        # Propagate CPU context into memory subsystem (if it supports it)
        mem = getattr(self.write_fn, '__self__', None)
        if mem is not None:
            try:
                setattr(mem, '_cpu_cycle', cyc)
                setattr(mem, '_cpu_pc', int(self.pc))
            except Exception:
                pass

        # Optional observer (SID-PRO capture)
        if self._write_observer is not None:
            try:
                self._write_observer(cyc, a, v)
            except Exception:
                pass

        # Verbose memory write logging
        if SystemLogger.category_enabled('memrw') or SystemLogger.category_enabled('memwrite') or SystemLogger.category_enabled('memio'):
            region = None
            try:
                if mem is not None and hasattr(mem, 'classify_address'):
                    region = mem.classify_address(a)
            except Exception:
                region = None

            cat = 'memrw' if SystemLogger.category_enabled('memrw') else 'memwrite'
            # Promote I/O routed writes to memio when enabled
            if region and isinstance(region, dict) and region.get('region', '').startswith(('SID', 'VIC', 'CIA')) and SystemLogger.category_enabled('memio'):
                cat = 'memio'

            SystemLogger.log(
                'MEMW',
                f"cy={cyc} pc=${self.pc:04X} WRITE @{a:04X} <- ${v:02X}" ,
                'debug',
                category=cat,
                fields={
                    'cycle': cyc,
                    'pc': int(self.pc),
                    'pc_hex': f"0x{int(self.pc) & 0xFFFF:04X}",
                    'addr': a,
                    'addr_hex': f"0x{int(a) & 0xFFFF:04X}",
                    'value': v,
                    'value_hex': f"0x{int(v) & 0xFF:02X}",
                    'region': (region.get('region') if isinstance(region, dict) else None),
                    'region_detail': region,
                },
            )

        # SID register decode at CPU boundary (includes cycle index)
        if mem is not None and hasattr(mem, 'sidBases') and hasattr(mem, 'sids'):
            try:
                bases = getattr(mem, 'sidBases', [])
                sids = getattr(mem, 'sids', [])
                for chip, base in enumerate(bases):
                    if int(base) <= a <= int(base) + 0x1F:
                        reg = a - int(base)
                        rn = get_sid_reg_name(reg)
                        clk = int(getattr(sids[chip], 'clock_hz', 985_248)) if chip < len(sids) else 985_248
                        ann = analyze_sid_write(reg, v, clock_freq=clk, cycle=(cyc if cyc >= 0 else None))

                        # Route to specialized categories for D418/filter/digi
                        if reg == 0x18:
                            if SystemLogger.category_enabled('sidvol') or SystemLogger.category_enabled('sidregs'):
                                SystemLogger.log(
                                    'D418',
                                    f"cy={cyc} pc=${self.pc:04X} chip={chip} @{a:04X} reg=0x18 {rn} <- ${v:02X} | {ann}",
                                    'debug',
                                    category='sidvol' if SystemLogger.category_enabled('sidvol') else 'sidregs',
                                    fields={
                                    'cycle': cyc,
                                    'pc': int(self.pc),
                                    'pc_hex': f"0x{int(self.pc) & 0xFFFF:04X}",
                                    'chip': chip,
                                    'addr': a,
                                    'addr_hex': f"0x{int(a) & 0xFFFF:04X}",
                                    'reg': reg,
                                    'reg_index': reg,
                                    'reg_hex': f"0x{int(reg) & 0xFF:02X}",
                                    'name': rn,
                                    'value': v,
                                    'value_hex': f"0x{int(v) & 0xFF:02X}",
                                    'annotation': ann,
                                },
                                )
                            if 'DIGI' in ann and SystemLogger.category_enabled('digi'):
                                SystemLogger.log(
                                    'DIGI',
                                    f"cy={cyc} pc=${self.pc:04X} chip={chip} @{a:04X} VOL/DAC <- ${v & 0x0F:X} | {ann}",
                                    'debug',
                                    category='digi',
                                    fields={
                                    'cycle': cyc,
                                    'pc': int(self.pc),
                                    'pc_hex': f"0x{int(self.pc) & 0xFFFF:04X}",
                                    'chip': chip,
                                    'addr': a,
                                    'addr_hex': f"0x{int(a) & 0xFFFF:04X}",
                                    'reg': reg,
                                    'reg_index': reg,
                                    'reg_hex': f"0x{int(reg) & 0xFF:02X}",
                                    'value': v,
                                    'value_hex': f"0x{int(v) & 0xFF:02X}",
                                    'annotation': ann,
                                },
                                )
                        elif reg in (0x15, 0x16, 0x17):
                            if SystemLogger.category_enabled('sidfilter'):
                                SystemLogger.log(
                                    'SIDFLT',
                                    f"cy={cyc} pc=${self.pc:04X} chip={chip} @{a:04X} reg=0x{reg:02X} {rn} <- ${v:02X} | {ann}",
                                    'debug',
                                    category='sidfilter',
                                    fields={
                                    'cycle': cyc,
                                    'pc': int(self.pc),
                                    'pc_hex': f"0x{int(self.pc) & 0xFFFF:04X}",
                                    'chip': chip,
                                    'addr': a,
                                    'addr_hex': f"0x{int(a) & 0xFFFF:04X}",
                                    'reg': reg,
                                    'reg_index': reg,
                                    'reg_hex': f"0x{int(reg) & 0xFF:02X}",
                                    'name': rn,
                                    'value': v,
                                    'value_hex': f"0x{int(v) & 0xFF:02X}",
                                    'annotation': ann,
                                },
                                )
                        else:
                            if SystemLogger.category_enabled('sidregs'):
                                SystemLogger.log(
                                    'SIDREG',
                                    f"cy={cyc} pc=${self.pc:04X} chip={chip} @{a:04X} reg=0x{reg:02X} {rn} <- ${v:02X} | {ann}",
                                    'debug',
                                    category='sidregs',
                                    fields={
                                    'cycle': cyc,
                                    'pc': int(self.pc),
                                    'pc_hex': f"0x{int(self.pc) & 0xFFFF:04X}",
                                    'chip': chip,
                                    'addr': a,
                                    'addr_hex': f"0x{int(a) & 0xFFFF:04X}",
                                    'reg': reg,
                                    'reg_index': reg,
                                    'reg_hex': f"0x{int(reg) & 0xFF:02X}",
                                    'name': rn,
                                    'value': v,
                                    'value_hex': f"0x{int(v) & 0xFF:02X}",
                                    'annotation': ann,
                                },
                                )
                        break
            except Exception:
                # Logging must never break emulation.
                pass

        self.write_fn(a, v)
        self._trace_tick(v)

    def read16(self, addr: int) -> int:
        lo = self.read(addr)
        hi = self.read(addr + 1)
        return (hi << 8) | lo

    def read16bug(self, addr: int) -> int:
        lo = self.read(addr)
        hi_addr = (addr & 0xFF00) | ((addr + 1) & 0x00FF)
        hi = self.read(hi_addr)
        return (hi << 8) | lo

    # --- Stack ---
    def push(self, v: int) -> None:
        self.write(0x0100 | self.sp, v)
        self.sp = _u8(self.sp - 1)

    def pull(self) -> int:
        self.sp = _u8(self.sp + 1)
        return self.read(0x0100 | self.sp)

    # --- Flags ---
    def get_flag(self, m: int) -> bool:
        return (self.flags & m) != 0

    def set_flag(self, m: int, on: bool) -> None:
        if on:
            self.flags |= m
        else:
            self.flags &= ~m

    def set_zn(self, v: int) -> None:
        v = _u8(v)
        self.set_flag(Cpu6502.Z, v == 0)
        self.set_flag(Cpu6502.N, (v & 0x80) != 0)

    # --- Reset/IRQ/NMI ---
    def reset(self, vector: int = 0xFFFC) -> None:
        self._trace_begin()
        self.a = 0
        self.x = 0
        self.y = 0
        self.sp = 0xFD
        self.flags = Cpu6502.U | Cpu6502.I
        self.pc = self.read16(vector)
        # Power-on reset: log the reset vector jump. (BRK flag does not apply here.)
        if SystemLogger.category_enabled('boot') and SystemLogger.rate_limit('CPU', 'reset_vector', 0.25):
            SystemLogger.log('CPU', f"RESET vector=${vector:04X} -> PC=${self.pc:04X}", 'debug', category='boot')
        # Reset takes 7 cycles
        eff = self._trace_end(7)
        self.cycles += eff

    def irq_vector(self, vector: int, brk: bool = False) -> int:
        self._trace_begin()
        # IRQ/NMI sequence includes a dummy read of the next opcode before stacking.
        self.read(self.pc)  # dummy read (prefetch) for IRQ/NMI timing
        self.push((self.pc >> 8) & 0xFF)
        self.push(self.pc & 0xFF)
        flags = self.flags | Cpu6502.U
        if brk:
            flags |= Cpu6502.B
        else:
            flags &= ~Cpu6502.B
        self.push(flags)
        self.set_flag(Cpu6502.I, True)
        # NMOS 6502/6510: D flag cleared on IRQ/NMI (not on BRK)
        if not brk:
            self.set_flag(Cpu6502.D, False)
        self.pc = self.read16(vector)
        if SystemLogger.category_enabled('boot') and SystemLogger.rate_limit('CPU', 'irq_vector', 0.25):
            SystemLogger.log('CPU', f"IRQ/NMI vector=${vector:04X} -> PC=${self.pc:04X} brk={brk}", 'debug', category='boot')
        eff = self._trace_end(7)
        return eff

    def irq(self) -> int:
        if self.get_flag(Cpu6502.I):
            return 0
        cyc = self.irq_vector(0xFFFE, False)
        self.cycles += cyc
        return cyc

    def nmi(self) -> int:
        cyc = self.irq_vector(0xFFFA, False)
        self.cycles += cyc
        return cyc

    # --- ALU ---
    def adc(self, v: int) -> None:
        v &= 0xFF
        a = self.a & 0xFF
        c = 1 if self.get_flag(Cpu6502.C) else 0

        if self.get_flag(Cpu6502.D):
            # BCD mode
            lo = (a & 0x0F) + (v & 0x0F) + c
            if lo > 0x09:
                lo += 0x06
            hi = (a >> 4) + (v >> 4) + (1 if lo > 0x0F else 0)
            if hi > 0x09:
                hi += 0x06
            res = ((hi & 0x0F) << 4) | (lo & 0x0F)
            self.set_flag(Cpu6502.C, hi > 0x0F)
            # V flag behavior in BCD is undefined, use binary calculation
            r = a + v + c
            self.set_flag(Cpu6502.V, (~(a ^ v) & (a ^ (r & 0xFF)) & 0x80) != 0)
            self.a = res & 0xFF
            self.set_zn(self.a)
        else:
            # Binary mode
            r = a + v + c
            res = r & 0xFF
            self.set_flag(Cpu6502.C, r > 0xFF)
            self.set_flag(Cpu6502.V, (~(a ^ v) & (a ^ res) & 0x80) != 0)
            self.a = res
            self.set_zn(self.a)

    def sbc(self, v: int) -> None:
        """SBC (Subtract with Borrow) with correct NMOS 6502/6510 decimal behavior.

        The old shortcut (ADC(~v)) is *not* correct for NMOS 6502/6510 in BCD mode.
        In decimal mode, the adjustment rules differ between ADC and SBC.

        Flags:
          - C: set if no borrow (i.e., A >= v + (1-Cin))
          - V: as binary subtract overflow
          - Z/N: from the final (post-adjust) result
        """
        v &= 0xFF
        a = self.a & 0xFF
        cin = 1 if self.get_flag(Cpu6502.C) else 0  # 1 = no borrow in

        # Binary subtract used for V and initial carry/borrow.
        bin_tmp = a - v - (1 - cin)
        bin_res = bin_tmp & 0xFF
        self.set_flag(Cpu6502.C, bin_tmp >= 0)
        self.set_flag(Cpu6502.V, ((a ^ bin_res) & (a ^ v) & 0x80) != 0)

        if self.get_flag(Cpu6502.D):
            # NMOS BCD adjust (6502/6510):
            # If low nibble borrow occurred, subtract 0x06.
            # If high digit borrow occurred, subtract 0x60.
            tmp = bin_tmp
            lo = (a & 0x0F) - (v & 0x0F) - (1 - cin)
            if lo < 0:
                tmp -= 0x06
            if tmp < 0:
                tmp -= 0x60
            res = tmp & 0xFF
            self.a = res
            self.set_zn(self.a)
        else:
            self.a = bin_res
            self.set_zn(self.a)

    def cmp(self, a: int, v: int) -> None:
        a &= 0xFF
        v &= 0xFF
        r = (a - v) & 0x1FF
        self.set_flag(Cpu6502.C, a >= v)
        self.set_zn(r & 0xFF)

    def branch(self, cond: bool) -> int:
        rel = self.fetch()
        if rel & 0x80:
            rel -= 0x100
        if not cond:
            return 2
        old = self.pc
        self.pc = _u16(self.pc + rel)
        cyc = 3
        if (old & 0xFF00) != (self.pc & 0xFF00):
            cyc += 1
        return cyc

    # --- Fetch/decode helpers ---
    def fetch(self) -> int:
        v = self.read(self.pc)
        self.pc = _u16(self.pc + 1)
        return v

    # addressing modes
    def _zp(self) -> int:
        return self.fetch()

    def _zpX(self) -> int:
        return _u8(self.fetch() + self.x)

    def _zpY(self) -> int:
        return _u8(self.fetch() + self.y)

    def _abs(self) -> int:
        lo = self.fetch(); hi = self.fetch()
        return (hi << 8) | lo

    def _absX(self) -> Tuple[int, bool]:
        base = self._abs()
        addr = _u16(base + self.x)
        return addr, (base & 0xFF00) != (addr & 0xFF00)

    def _absY(self) -> Tuple[int, bool]:
        base = self._abs()
        addr = _u16(base + self.y)
        return addr, (base & 0xFF00) != (addr & 0xFF00)

    def _indX(self) -> int:
        zp = _u8(self.fetch() + self.x)
        return self.read(zp) | (self.read(_u8(zp + 1)) << 8)

    def _indY(self) -> Tuple[int, bool]:
        zp = self.fetch()
        base = self.read(zp) | (self.read(_u8(zp + 1)) << 8)
        addr = _u16(base + self.y)
        return addr, (base & 0xFF00) != (addr & 0xFF00)

    # --- Execute one instruction ---
    def step(self) -> int:
        self._trace_begin()
        op = self.fetch()
        cycles = 2

        # shift/rotate helpers
        def asl(v: int) -> int:
            v &= 0xFF
            self.set_flag(Cpu6502.C, (v & 0x80) != 0)
            v = (v << 1) & 0xFF
            self.set_zn(v)
            return v

        def lsr(v: int) -> int:
            v &= 0xFF
            self.set_flag(Cpu6502.C, (v & 0x01) != 0)
            v = (v >> 1) & 0xFF
            self.set_zn(v)
            return v

        def rol(v: int) -> int:
            v &= 0xFF
            c = 1 if self.get_flag(Cpu6502.C) else 0
            self.set_flag(Cpu6502.C, (v & 0x80) != 0)
            v = ((v << 1) | c) & 0xFF
            self.set_zn(v)
            return v

        def ror(v: int) -> int:
            v &= 0xFF
            c = 0x80 if self.get_flag(Cpu6502.C) else 0
            self.set_flag(Cpu6502.C, (v & 0x01) != 0)
            v = ((v >> 1) | c) & 0xFF
            self.set_zn(v)
            return v

        # --- Decode & execute ---
        if op == 0x00:
            # BRK is 2 bytes; second byte is padding. Pushes PC+2.
            # Cycle-accurate bus sequence is constructed here without resetting trace counters.
            self.read(self.pc)          # padding byte fetch
            self.pc = _u16(self.pc + 1) # advance to next instruction (PC+2 total)
            self.push((self.pc >> 8) & 0xFF)
            self.push(self.pc & 0xFF)
            flags = (self.flags | Cpu6502.U | Cpu6502.B) & 0xFF
            self.push(flags)
            self.set_flag(Cpu6502.I, True)
            self.pc = self.read16(0xFFFE)
            cycles = 7

        elif op == 0x40:
            self.flags = (self.pull() | Cpu6502.U) & ~Cpu6502.B
            lo = self.pull(); hi = self.pull()
            self.pc = (hi << 8) | lo
            cycles = 6

        elif op == 0x60:
            lo = self.pull(); hi = self.pull()
            self.pc = _u16(((hi << 8) | lo) + 1)
            cycles = 6

        elif op == 0xEA:
            cycles = 2

        # Flag ops
        elif op == 0x18:
            self.set_flag(Cpu6502.C, False); cycles = 2
        elif op == 0x38:
            self.set_flag(Cpu6502.C, True); cycles = 2
        elif op == 0x58:
            self.set_flag(Cpu6502.I, False); cycles = 2
        elif op == 0x78:
            self.set_flag(Cpu6502.I, True); cycles = 2
        elif op == 0xB8:
            self.set_flag(Cpu6502.V, False); cycles = 2
        elif op == 0xD8:
            self.set_flag(Cpu6502.D, False); cycles = 2
        elif op == 0xF8:
            self.set_flag(Cpu6502.D, True); cycles = 2

        # Stack
        elif op == 0x48:
            self.push(self.a); cycles = 3
        elif op == 0x68:
            self.a = self.pull(); self.set_zn(self.a); cycles = 4
        elif op == 0x08:
            self.push(self.flags | Cpu6502.B | Cpu6502.U); cycles = 3
        elif op == 0x28:
            self.flags = (self.pull() | Cpu6502.U) & ~Cpu6502.B; cycles = 4

        # Transfers
        elif op == 0xAA:
            self.x = self.a; self.set_zn(self.x); cycles = 2
        elif op == 0xA8:
            self.y = self.a; self.set_zn(self.y); cycles = 2
        elif op == 0x8A:
            self.a = self.x; self.set_zn(self.a); cycles = 2
        elif op == 0x98:
            self.a = self.y; self.set_zn(self.a); cycles = 2
        elif op == 0xBA:
            self.x = self.sp; self.set_zn(self.x); cycles = 2
        elif op == 0x9A:
            self.sp = self.x; cycles = 2

        # INC/DEC regs
        elif op == 0xE8:
            self.x = _u8(self.x + 1); self.set_zn(self.x); cycles = 2
        elif op == 0xC8:
            self.y = _u8(self.y + 1); self.set_zn(self.y); cycles = 2
        elif op == 0xCA:
            self.x = _u8(self.x - 1); self.set_zn(self.x); cycles = 2
        elif op == 0x88:
            self.y = _u8(self.y - 1); self.set_zn(self.y); cycles = 2

        # JSR/JMP
        elif op == 0x20:
            addr = self._abs()
            ret = _u16(self.pc - 1)
            self.push((ret >> 8) & 0xFF)
            self.push(ret & 0xFF)
            self.pc = addr
            cycles = 6
        elif op == 0x4C:
            self.pc = self._abs(); cycles = 3
        elif op == 0x6C:
            ptr = self._abs(); self.pc = self.read16bug(ptr); cycles = 5

        # Branches
        elif op == 0x10: cycles = self.branch(not self.get_flag(Cpu6502.N))
        elif op == 0x30: cycles = self.branch(self.get_flag(Cpu6502.N))
        elif op == 0x50: cycles = self.branch(not self.get_flag(Cpu6502.V))
        elif op == 0x70: cycles = self.branch(self.get_flag(Cpu6502.V))
        elif op == 0x90: cycles = self.branch(not self.get_flag(Cpu6502.C))
        elif op == 0xB0: cycles = self.branch(self.get_flag(Cpu6502.C))
        elif op == 0xD0: cycles = self.branch(not self.get_flag(Cpu6502.Z))
        elif op == 0xF0: cycles = self.branch(self.get_flag(Cpu6502.Z))

        # BIT
        elif op == 0x24:
            a = self._zp()
            v = self.read(a)
            self.set_flag(Cpu6502.Z, (self.a & v) == 0)
            self.set_flag(Cpu6502.N, (v & 0x80) != 0)
            self.set_flag(Cpu6502.V, (v & 0x40) != 0)
            cycles = 3
        elif op == 0x2C:
            a = self._abs()
            v = self.read(a)
            self.set_flag(Cpu6502.Z, (self.a & v) == 0)
            self.set_flag(Cpu6502.N, (v & 0x80) != 0)
            self.set_flag(Cpu6502.V, (v & 0x40) != 0)
            cycles = 4

        # Loads: LDA
        elif op == 0xA9:
            self.a = self.fetch(); self.set_zn(self.a); cycles = 2
        elif op == 0xA5:
            a = self._zp(); self.a = self.read(a); self.set_zn(self.a); cycles = 3
        elif op == 0xB5:
            a = self._zpX(); self.a = self.read(a); self.set_zn(self.a); cycles = 4
        elif op == 0xAD:
            a = self._abs(); self.a = self.read(a); self.set_zn(self.a); cycles = 4
        elif op == 0xBD:
            addr, cross = self._absX(); self.a = self.read(addr); self.set_zn(self.a); cycles = 4 + (1 if cross else 0)
        elif op == 0xB9:
            addr, cross = self._absY(); self.a = self.read(addr); self.set_zn(self.a); cycles = 4 + (1 if cross else 0)
        elif op == 0xA1:
            a = self._indX(); self.a = self.read(a); self.set_zn(self.a); cycles = 6
        elif op == 0xB1:
            addr, cross = self._indY(); self.a = self.read(addr); self.set_zn(self.a); cycles = 5 + (1 if cross else 0)

        # Loads: LDX
        elif op == 0xA2:
            self.x = self.fetch(); self.set_zn(self.x); cycles = 2
        elif op == 0xA6:
            a = self._zp(); self.x = self.read(a); self.set_zn(self.x); cycles = 3
        elif op == 0xB6:
            a = self._zpY(); self.x = self.read(a); self.set_zn(self.x); cycles = 4
        elif op == 0xAE:
            a = self._abs(); self.x = self.read(a); self.set_zn(self.x); cycles = 4
        elif op == 0xBE:
            addr, cross = self._absY(); self.x = self.read(addr); self.set_zn(self.x); cycles = 4 + (1 if cross else 0)

        # Loads: LDY
        elif op == 0xA0:
            self.y = self.fetch(); self.set_zn(self.y); cycles = 2
        elif op == 0xA4:
            a = self._zp(); self.y = self.read(a); self.set_zn(self.y); cycles = 3
        elif op == 0xB4:
            a = self._zpX(); self.y = self.read(a); self.set_zn(self.y); cycles = 4
        elif op == 0xAC:
            a = self._abs(); self.y = self.read(a); self.set_zn(self.y); cycles = 4
        elif op == 0xBC:
            addr, cross = self._absX(); self.y = self.read(addr); self.set_zn(self.y); cycles = 4 + (1 if cross else 0)

        # Stores: STA
        elif op == 0x85:
            a = self._zp(); self.write(a, self.a); cycles = 3
        elif op == 0x95:
            a = self._zpX(); self.write(a, self.a); cycles = 4
        elif op == 0x8D:
            a = self._abs(); self.write(a, self.a); cycles = 4
        elif op == 0x9D:
            addr, _ = self._absX(); self.write(addr, self.a); cycles = 5
        elif op == 0x99:
            addr, _ = self._absY(); self.write(addr, self.a); cycles = 5
        elif op == 0x81:
            a = self._indX(); self.write(a, self.a); cycles = 6
        elif op == 0x91:
            addr, _ = self._indY(); self.write(addr, self.a); cycles = 6

        # Stores: STX
        elif op == 0x86:
            a = self._zp(); self.write(a, self.x); cycles = 3
        elif op == 0x96:
            a = self._zpY(); self.write(a, self.x); cycles = 4
        elif op == 0x8E:
            a = self._abs(); self.write(a, self.x); cycles = 4

        # Stores: STY
        elif op == 0x84:
            a = self._zp(); self.write(a, self.y); cycles = 3
        elif op == 0x94:
            a = self._zpX(); self.write(a, self.y); cycles = 4
        elif op == 0x8C:
            a = self._abs(); self.write(a, self.y); cycles = 4

        # ORA
        elif op == 0x09:
            self.a = _u8(self.a | self.fetch()); self.set_zn(self.a); cycles = 2
        elif op == 0x05:
            a = self._zp(); self.a = _u8(self.a | self.read(a)); self.set_zn(self.a); cycles = 3
        elif op == 0x15:
            a = self._zpX(); self.a = _u8(self.a | self.read(a)); self.set_zn(self.a); cycles = 4
        elif op == 0x0D:
            a = self._abs(); self.a = _u8(self.a | self.read(a)); self.set_zn(self.a); cycles = 4
        elif op == 0x1D:
            addr, cross = self._absX(); self.a = _u8(self.a | self.read(addr)); self.set_zn(self.a); cycles = 4 + (1 if cross else 0)
        elif op == 0x19:
            addr, cross = self._absY(); self.a = _u8(self.a | self.read(addr)); self.set_zn(self.a); cycles = 4 + (1 if cross else 0)
        elif op == 0x01:
            a = self._indX(); self.a = _u8(self.a | self.read(a)); self.set_zn(self.a); cycles = 6
        elif op == 0x11:
            addr, cross = self._indY(); self.a = _u8(self.a | self.read(addr)); self.set_zn(self.a); cycles = 5 + (1 if cross else 0)

        # AND
        elif op == 0x29:
            self.a = _u8(self.a & self.fetch()); self.set_zn(self.a); cycles = 2
        elif op == 0x25:
            a = self._zp(); self.a = _u8(self.a & self.read(a)); self.set_zn(self.a); cycles = 3
        elif op == 0x35:
            a = self._zpX(); self.a = _u8(self.a & self.read(a)); self.set_zn(self.a); cycles = 4
        elif op == 0x2D:
            a = self._abs(); self.a = _u8(self.a & self.read(a)); self.set_zn(self.a); cycles = 4
        elif op == 0x3D:
            addr, cross = self._absX(); self.a = _u8(self.a & self.read(addr)); self.set_zn(self.a); cycles = 4 + (1 if cross else 0)
        elif op == 0x39:
            addr, cross = self._absY(); self.a = _u8(self.a & self.read(addr)); self.set_zn(self.a); cycles = 4 + (1 if cross else 0)
        elif op == 0x21:
            a = self._indX(); self.a = _u8(self.a & self.read(a)); self.set_zn(self.a); cycles = 6
        elif op == 0x31:
            addr, cross = self._indY(); self.a = _u8(self.a & self.read(addr)); self.set_zn(self.a); cycles = 5 + (1 if cross else 0)

        # EOR
        elif op == 0x49:
            self.a = _u8(self.a ^ self.fetch()); self.set_zn(self.a); cycles = 2
        elif op == 0x45:
            a = self._zp(); self.a = _u8(self.a ^ self.read(a)); self.set_zn(self.a); cycles = 3
        elif op == 0x55:
            a = self._zpX(); self.a = _u8(self.a ^ self.read(a)); self.set_zn(self.a); cycles = 4
        elif op == 0x4D:
            a = self._abs(); self.a = _u8(self.a ^ self.read(a)); self.set_zn(self.a); cycles = 4
        elif op == 0x5D:
            addr, cross = self._absX(); self.a = _u8(self.a ^ self.read(addr)); self.set_zn(self.a); cycles = 4 + (1 if cross else 0)
        elif op == 0x59:
            addr, cross = self._absY(); self.a = _u8(self.a ^ self.read(addr)); self.set_zn(self.a); cycles = 4 + (1 if cross else 0)
        elif op == 0x41:
            a = self._indX(); self.a = _u8(self.a ^ self.read(a)); self.set_zn(self.a); cycles = 6
        elif op == 0x51:
            addr, cross = self._indY(); self.a = _u8(self.a ^ self.read(addr)); self.set_zn(self.a); cycles = 5 + (1 if cross else 0)

        # ADC
        elif op == 0x69:
            self.adc(self.fetch()); cycles = 2
        elif op == 0x65:
            a = self._zp(); self.adc(self.read(a)); cycles = 3
        elif op == 0x75:
            a = self._zpX(); self.adc(self.read(a)); cycles = 4
        elif op == 0x6D:
            a = self._abs(); self.adc(self.read(a)); cycles = 4
        elif op == 0x7D:
            addr, cross = self._absX(); self.adc(self.read(addr)); cycles = 4 + (1 if cross else 0)
        elif op == 0x79:
            addr, cross = self._absY(); self.adc(self.read(addr)); cycles = 4 + (1 if cross else 0)
        elif op == 0x61:
            a = self._indX(); self.adc(self.read(a)); cycles = 6
        elif op == 0x71:
            addr, cross = self._indY(); self.adc(self.read(addr)); cycles = 5 + (1 if cross else 0)

        # SBC
        elif op == 0xE9:
            self.sbc(self.fetch()); cycles = 2
        elif op == 0xE5:
            a = self._zp(); self.sbc(self.read(a)); cycles = 3
        elif op == 0xF5:
            a = self._zpX(); self.sbc(self.read(a)); cycles = 4
        elif op == 0xED:
            a = self._abs(); self.sbc(self.read(a)); cycles = 4
        elif op == 0xFD:
            addr, cross = self._absX(); self.sbc(self.read(addr)); cycles = 4 + (1 if cross else 0)
        elif op == 0xF9:
            addr, cross = self._absY(); self.sbc(self.read(addr)); cycles = 4 + (1 if cross else 0)
        elif op == 0xE1:
            a = self._indX(); self.sbc(self.read(a)); cycles = 6
        elif op == 0xF1:
            addr, cross = self._indY(); self.sbc(self.read(addr)); cycles = 5 + (1 if cross else 0)

        # BIT
        elif op == 0x24:
            a = self._zp(); v = self.read(a); self.set_flag(Cpu6502.Z, (self.a & v) == 0); self.set_flag(Cpu6502.V, (v & 0x40) != 0); self.set_flag(Cpu6502.N, (v & 0x80) != 0); cycles = 3
        elif op == 0x2C:
            a = self._abs(); v = self.read(a); self.set_flag(Cpu6502.Z, (self.a & v) == 0); self.set_flag(Cpu6502.V, (v & 0x40) != 0); self.set_flag(Cpu6502.N, (v & 0x80) != 0); cycles = 4

        # CMP/CPX/CPY
        elif op == 0xC9:
            self.cmp(self.a, self.fetch()); cycles = 2
        elif op == 0xC5:
            a = self._zp(); self.cmp(self.a, self.read(a)); cycles = 3
        elif op == 0xD5:
            a = self._zpX(); self.cmp(self.a, self.read(a)); cycles = 4
        elif op == 0xCD:
            a = self._abs(); self.cmp(self.a, self.read(a)); cycles = 4
        elif op == 0xDD:
            addr, cross = self._absX(); self.cmp(self.a, self.read(addr)); cycles = 4 + (1 if cross else 0)
        elif op == 0xD9:
            addr, cross = self._absY(); self.cmp(self.a, self.read(addr)); cycles = 4 + (1 if cross else 0)
        elif op == 0xC1:
            a = self._indX(); self.cmp(self.a, self.read(a)); cycles = 6
        elif op == 0xD1:
            addr, cross = self._indY(); self.cmp(self.a, self.read(addr)); cycles = 5 + (1 if cross else 0)

        elif op == 0xE0:
            self.cmp(self.x, self.fetch()); cycles = 2
        elif op == 0xE4:
            a = self._zp(); self.cmp(self.x, self.read(a)); cycles = 3
        elif op == 0xEC:
            a = self._abs(); self.cmp(self.x, self.read(a)); cycles = 4

        elif op == 0xC0:
            self.cmp(self.y, self.fetch()); cycles = 2
        elif op == 0xC4:
            a = self._zp(); self.cmp(self.y, self.read(a)); cycles = 3
        elif op == 0xCC:
            a = self._abs(); self.cmp(self.y, self.read(a)); cycles = 4

        # INC/DEC memory
        elif op == 0xE6:
            a = self._zp(); v = _u8(self.read(a) + 1); self.write(a, v); self.set_zn(v); cycles = 5
        elif op == 0xF6:
            a = self._zpX(); v = _u8(self.read(a) + 1); self.write(a, v); self.set_zn(v); cycles = 6
        elif op == 0xEE:
            a = self._abs(); v = _u8(self.read(a) + 1); self.write(a, v); self.set_zn(v); cycles = 6
        elif op == 0xFE:
            addr, _ = self._absX(); v = _u8(self.read(addr) + 1); self.write(addr, v); self.set_zn(v); cycles = 7

        elif op == 0xC6:
            a = self._zp(); v = _u8(self.read(a) - 1); self.write(a, v); self.set_zn(v); cycles = 5
        elif op == 0xD6:
            a = self._zpX(); v = _u8(self.read(a) - 1); self.write(a, v); self.set_zn(v); cycles = 6
        elif op == 0xCE:
            a = self._abs(); v = _u8(self.read(a) - 1); self.write(a, v); self.set_zn(v); cycles = 6
        elif op == 0xDE:
            addr, _ = self._absX(); v = _u8(self.read(addr) - 1); self.write(addr, v); self.set_zn(v); cycles = 7

        # ASL/LSR/ROL/ROR accumulator
        elif op == 0x0A:
            self.a = asl(self.a); cycles = 2
        elif op == 0x4A:
            self.a = lsr(self.a); cycles = 2
        elif op == 0x2A:
            self.a = rol(self.a); cycles = 2
        elif op == 0x6A:
            self.a = ror(self.a); cycles = 2

        # ASL memory
        elif op == 0x06:
            a = self._zp(); v = asl(self.read(a)); self.write(a, v); cycles = 5
        elif op == 0x16:
            a = self._zpX(); v = asl(self.read(a)); self.write(a, v); cycles = 6
        elif op == 0x0E:
            a = self._abs(); v = asl(self.read(a)); self.write(a, v); cycles = 6
        elif op == 0x1E:
            addr, _ = self._absX(); v = asl(self.read(addr)); self.write(addr, v); cycles = 7

        # LSR memory
        elif op == 0x46:
            a = self._zp(); v = lsr(self.read(a)); self.write(a, v); cycles = 5
        elif op == 0x56:
            a = self._zpX(); v = lsr(self.read(a)); self.write(a, v); cycles = 6
        elif op == 0x4E:
            a = self._abs(); v = lsr(self.read(a)); self.write(a, v); cycles = 6
        elif op == 0x5E:
            addr, _ = self._absX(); v = lsr(self.read(addr)); self.write(addr, v); cycles = 7

        # ROL memory
        elif op == 0x26:
            a = self._zp(); v = rol(self.read(a)); self.write(a, v); cycles = 5
        elif op == 0x36:
            a = self._zpX(); v = rol(self.read(a)); self.write(a, v); cycles = 6
        elif op == 0x2E:
            a = self._abs(); v = rol(self.read(a)); self.write(a, v); cycles = 6
        elif op == 0x3E:
            addr, _ = self._absX(); v = rol(self.read(addr)); self.write(addr, v); cycles = 7

        # ROR memory
        elif op == 0x66:
            a = self._zp(); v = ror(self.read(a)); self.write(a, v); cycles = 5
        elif op == 0x76:
            a = self._zpX(); v = ror(self.read(a)); self.write(a, v); cycles = 6
        elif op == 0x6E:
            a = self._abs(); v = ror(self.read(a)); self.write(a, v); cycles = 6
        elif op == 0x7E:
            addr, _ = self._absX(); v = ror(self.read(addr)); self.write(addr, v); cycles = 7

        else:
            info = CpuIllegals.get_instruction_info(op)
            if info:
                addr = 0
                val = 0

                # Immediate mode opcodes (ANC, ALR, ARR, AXS)
                if op in (0x0B, 0x2B, 0x4B, 0x6B, 0xCB):
                    val = self.fetch()
                # No-operand illegals (KIL/JAM)
                elif op in (0x02, 0x12, 0x22, 0x32, 0x42, 0x52, 0x62, 0x72, 0x92, 0xB2, 0xD2, 0xF2):
                    addr = 0
                    val = 0
                # Decode addressing mode by exact opcode sets
                # zp
                elif op in (0xA7, 0xC7, 0xE7, 0x07, 0x27, 0x47, 0x67, 0x87):
                    addr = self._zp(); val = self.read(addr)
                # zp,X (RMW illegals)
                elif op in (0xD7, 0xF7, 0x17, 0x37, 0x57, 0x77):
                    addr = self._zpX(); val = self.read(addr)
                # zp,Y (LAX/SAX)
                elif op in (0xB7, 0x97):
                    addr = self._zpY(); val = self.read(addr)
                # abs
                elif op in (0xAF, 0xCF, 0xEF, 0x0F, 0x2F, 0x4F, 0x6F, 0x8F):
                    addr = self._abs(); val = self.read(addr)
                # abs,X
                elif op in (0xDF, 0xFF, 0x1F, 0x3F, 0x5F, 0x7F, 0x9C):
                    addr, _ = self._absX(); val = self.read(addr)
                # abs,Y (includes LAS)
                elif op in (0xBF, 0xDB, 0xFB, 0x1B, 0x3B, 0x5B, 0x7B, 0xBB, 0x9E, 0x9F, 0x9B):
                    addr, _ = self._absY(); val = self.read(addr)
                # (ind,X)
                elif op in (0xA3, 0xC3, 0xE3, 0x03, 0x23, 0x43, 0x63, 0x83):
                    addr = self._indX(); val = self.read(addr)
                # (ind),Y
                elif op in (0xB3, 0xD3, 0xF3, 0x13, 0x33, 0x53, 0x73, 0x93):
                    addr, _ = self._indY(); val = self.read(addr)
                else:
                    cycles = 2
                    # fall through

                CpuIllegals.execute(self, op, addr, val)
                cycles = info.cycles if info.cycles is not None else cycles
            else:
                # Handle NOP variants with correct cycle timing
                if op in (0x1A, 0x3A, 0x5A, 0x7A, 0xDA, 0xEA, 0xFA):
                    # 1-byte NOPs (2 cycles)
                    cycles = 2
                elif op in (0x80, 0x82, 0x89, 0xC2, 0xE2):
                    # 2-byte NOPs with immediate (2 cycles)
                    self.fetch()
                    cycles = 2
                elif op in (0x04, 0x44, 0x64):
                    # 2-byte NOPs with zero page (3 cycles)
                    self._zp()
                    cycles = 3
                elif op in (0x14, 0x34, 0x54, 0x74, 0xD4, 0xF4):
                    # 2-byte NOPs with zero page,X (4 cycles)
                    self._zpX()
                    cycles = 4
                elif op in (0x0C,):
                    # 3-byte NOP with absolute (4 cycles)
                    self._abs()
                    cycles = 4
                elif op in (0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC):
                    # 3-byte NOPs with absolute,X (4 or 5 cycles with page cross)
                    addr, cross = self._absX()
                    cycles = 4 + (1 if cross else 0)
                else:
                    # Truly unknown opcodes - treat as 2-cycle NOP
                    cycles = 2

        cycles = self._trace_end(cycles)
        self.cycles += cycles
        return cycles
