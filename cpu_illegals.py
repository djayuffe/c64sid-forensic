from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .cpu6502 import Cpu6502


@dataclass(frozen=True)
class OpInfo:
    mnemonic: str
    size: int
    cycles: Optional[int] = None


class CpuIllegals:
    """6502 illegal opcode helpers.

    This expands the TS set (which only executed LAX/SAX) with the common RMW+ALU illegals
    used by many C64 codebases: DCP/ISC/SLO/RLA/SRE/RRA.
    """

    OP_INFO: Dict[int, OpInfo] = {
        # LAX
        0xA7: OpInfo('LAX', 2, 3), 0xB7: OpInfo('LAX', 2, 4), 0xAF: OpInfo('LAX', 3, 4), 0xBF: OpInfo('LAX', 3, 4), 0xA3: OpInfo('LAX', 2, 6), 0xB3: OpInfo('LAX', 2, 5),
        # SAX
        0x87: OpInfo('SAX', 2, 3), 0x97: OpInfo('SAX', 2, 4), 0x8F: OpInfo('SAX', 3, 4), 0x83: OpInfo('SAX', 2, 6),
        # DCP
        0xC7: OpInfo('DCP', 2, 5), 0xD7: OpInfo('DCP', 2, 6), 0xCF: OpInfo('DCP', 3, 6), 0xDF: OpInfo('DCP', 3, 7), 0xDB: OpInfo('DCP', 3, 7), 0xC3: OpInfo('DCP', 2, 8), 0xD3: OpInfo('DCP', 2, 8),
        # ISC (a.k.a. ISB)
        0xE7: OpInfo('ISC', 2, 5), 0xF7: OpInfo('ISC', 2, 6), 0xEF: OpInfo('ISC', 3, 6), 0xFF: OpInfo('ISC', 3, 7), 0xFB: OpInfo('ISC', 3, 7), 0xE3: OpInfo('ISC', 2, 8), 0xF3: OpInfo('ISC', 2, 8),
        # SLO
        0x07: OpInfo('SLO', 2, 5), 0x17: OpInfo('SLO', 2, 6), 0x0F: OpInfo('SLO', 3, 6), 0x1F: OpInfo('SLO', 3, 7), 0x1B: OpInfo('SLO', 3, 7), 0x03: OpInfo('SLO', 2, 8), 0x13: OpInfo('SLO', 2, 8),
        # RLA
        0x27: OpInfo('RLA', 2, 5), 0x37: OpInfo('RLA', 2, 6), 0x2F: OpInfo('RLA', 3, 6), 0x3F: OpInfo('RLA', 3, 7), 0x3B: OpInfo('RLA', 3, 7), 0x23: OpInfo('RLA', 2, 8), 0x33: OpInfo('RLA', 2, 8),
        # SRE
        0x47: OpInfo('SRE', 2, 5), 0x57: OpInfo('SRE', 2, 6), 0x4F: OpInfo('SRE', 3, 6), 0x5F: OpInfo('SRE', 3, 7), 0x5B: OpInfo('SRE', 3, 7), 0x43: OpInfo('SRE', 2, 8), 0x53: OpInfo('SRE', 2, 8),
        # RRA
        0x67: OpInfo('RRA', 2, 5), 0x77: OpInfo('RRA', 2, 6), 0x6F: OpInfo('RRA', 3, 6), 0x7F: OpInfo('RRA', 3, 7), 0x7B: OpInfo('RRA', 3, 7), 0x63: OpInfo('RRA', 2, 8), 0x73: OpInfo('RRA', 2, 8),
        # ANC (AND with carry set)
        0x0B: OpInfo('ANC', 2, 2), 0x2B: OpInfo('ANC', 2, 2),
        # ALR (AND + LSR)
        0x4B: OpInfo('ALR', 2, 2),
        # ARR (AND + ROR)
        0x6B: OpInfo('ARR', 2, 2),
        # AXS (A&X - immediate, no borrow)
        0xCB: OpInfo('AXS', 2, 2),
        # LAS
        0xBB: OpInfo('LAS', 3, 4),

        # SHY/SHX (a.k.a. SYA/SXA) - unstable on real NMOS, but widely used in C64 code
        0x9C: OpInfo('SHY', 3, 5),  # abs,X
        0x9E: OpInfo('SHX', 3, 5),  # abs,Y

        # AHX (a.k.a. SHA) - store A&X&(hi+1)
        0x93: OpInfo('AHX', 2, 6),  # (ind),Y
        0x9F: OpInfo('AHX', 3, 5),  # abs,Y

        # TAS (a.k.a. SHS) - SP=A&X, store SP&(hi+1)
        0x9B: OpInfo('TAS', 3, 5),  # abs,Y

        # KIL/JAM - halts CPU (we emulate as 2-cycle NOP with a warning unless configured)
        0x02: OpInfo('KIL', 1, 2), 0x12: OpInfo('KIL', 1, 2), 0x22: OpInfo('KIL', 1, 2), 0x32: OpInfo('KIL', 1, 2),
        0x42: OpInfo('KIL', 1, 2), 0x52: OpInfo('KIL', 1, 2), 0x62: OpInfo('KIL', 1, 2), 0x72: OpInfo('KIL', 1, 2),
        0x92: OpInfo('KIL', 1, 2), 0xB2: OpInfo('KIL', 1, 2), 0xD2: OpInfo('KIL', 1, 2), 0xF2: OpInfo('KIL', 1, 2),
    }

    @staticmethod
    def get_instruction_info(op: int) -> Optional[OpInfo]:
        return CpuIllegals.OP_INFO.get(op & 0xFF)

    @staticmethod
    def execute(cpu: 'Cpu6502', op: int, addr: int, val: int) -> None:
        op &= 0xFF
        val &= 0xFF

        # Helper lambdas using cpu's internal ALU
        def _asl_mem(v: int) -> int:
            cpu.set_flag(cpu.C, (v & 0x80) != 0)
            v = (v << 1) & 0xFF
            cpu.set_zn(v)
            return v

        def _lsr_mem(v: int) -> int:
            cpu.set_flag(cpu.C, (v & 0x01) != 0)
            v = (v >> 1) & 0xFF
            cpu.set_zn(v)
            return v

        def _rol_mem(v: int) -> int:
            c = 1 if cpu.get_flag(cpu.C) else 0
            cpu.set_flag(cpu.C, (v & 0x80) != 0)
            v = ((v << 1) | c) & 0xFF
            cpu.set_zn(v)
            return v

        def _ror_mem(v: int) -> int:
            c = 0x80 if cpu.get_flag(cpu.C) else 0
            cpu.set_flag(cpu.C, (v & 0x01) != 0)
            v = ((v >> 1) | c) & 0xFF
            cpu.set_zn(v)
            return v

        info = CpuIllegals.get_instruction_info(op)
        if not info:
            return

        m = info.mnemonic
        if m == 'LAX':
            cpu.a = val
            cpu.x = val
            cpu.set_zn(cpu.a)
            return

        if m == 'SAX':
            cpu.write(addr, cpu.a & cpu.x)
            return

        if m == 'DCP':
            v2 = (val - 1) & 0xFF
            cpu.write(addr, v2)
            cpu.cmp(cpu.a, v2)
            return

        if m == 'ISC':
            v2 = (val + 1) & 0xFF
            cpu.write(addr, v2)
            cpu.sbc(v2)
            return

        if m == 'SLO':
            v2 = _asl_mem(val)
            cpu.write(addr, v2)
            cpu.a = (cpu.a | v2) & 0xFF
            cpu.set_zn(cpu.a)
            return

        if m == 'RLA':
            v2 = _rol_mem(val)
            cpu.write(addr, v2)
            cpu.a = (cpu.a & v2) & 0xFF
            cpu.set_zn(cpu.a)
            return

        if m == 'SRE':
            v2 = _lsr_mem(val)
            cpu.write(addr, v2)
            cpu.a = (cpu.a ^ v2) & 0xFF
            cpu.set_zn(cpu.a)
            return

        if m == 'RRA':
            v2 = _ror_mem(val)
            cpu.write(addr, v2)
            cpu.adc(v2)
            return

        if m == 'ANC':
            cpu.a = (cpu.a & val) & 0xFF
            cpu.set_zn(cpu.a)
            cpu.set_flag(cpu.C, (cpu.a & 0x80) != 0)
            return

        if m == 'ALR':
            cpu.a = (cpu.a & val) & 0xFF
            cpu.set_flag(cpu.C, (cpu.a & 0x01) != 0)
            cpu.a = (cpu.a >> 1) & 0xFF
            cpu.set_zn(cpu.a)
            return

        if m == 'ARR':
            cpu.a = (cpu.a & val) & 0xFF
            c = 0x80 if cpu.get_flag(cpu.C) else 0
            cpu.a = ((cpu.a >> 1) | c) & 0xFF
            cpu.set_zn(cpu.a)
            cpu.set_flag(cpu.C, (cpu.a & 0x40) != 0)
            cpu.set_flag(cpu.V, bool(((cpu.a & 0x40) ^ ((cpu.a & 0x20) << 1))))
            return

        if m == 'AXS':
            result = ((cpu.a & cpu.x) - val) & 0x1FF
            cpu.x = result & 0xFF
            cpu.set_flag(cpu.C, result < 0x100)
            cpu.set_zn(cpu.x)
            return

        if m == 'LAS':
            v2 = val & cpu.sp
            cpu.a = v2
            cpu.x = v2
            cpu.sp = v2
            cpu.set_zn(v2)
            return

        # The following are "unstable" on real NMOS (value depends on bus),
        # but the standard C64-compatible formulas match what most code expects.
        # We implement the common documented behavior used by C64 assemblers/emus.

        if m == 'SHY':
            # Store Y & (hi(addr)+1)
            hi = ((addr >> 8) & 0xFF)
            cpu.write(addr, cpu.y & ((hi + 1) & 0xFF))
            return

        if m == 'SHX':
            # Store X & (hi(addr)+1)
            hi = ((addr >> 8) & 0xFF)
            cpu.write(addr, cpu.x & ((hi + 1) & 0xFF))
            return

        if m == 'AHX':
            # Store A & X & (hi(addr)+1)
            hi = ((addr >> 8) & 0xFF)
            cpu.write(addr, cpu.a & cpu.x & ((hi + 1) & 0xFF))
            return

        if m == 'TAS':
            # SP = A&X, store SP & (hi(addr)+1)
            sp = cpu.a & cpu.x
            cpu.sp = sp
            hi = ((addr >> 8) & 0xFF)
            cpu.write(addr, sp & ((hi + 1) & 0xFF))
            return

        if m == 'KIL':
            # True KIL/JAM halts the CPU. We can't hard-freeze the renderer (it would
            # lock up audio export), so we warn and behave like a 2-cycle NOP.
            # If you need strict jam behavior, detect the opcode at a higher layer.
            try:
                from .logger import SystemLogger
                if SystemLogger.rate_limit('CPU', 'kil', 1.0):
                    SystemLogger.log('CPU', f'KIL/JAM opcode ${op:02X} encountered; continuing (emulated as NOP)', 'warn', category='cpu')
            except Exception:
                pass
            return

        # (end)
