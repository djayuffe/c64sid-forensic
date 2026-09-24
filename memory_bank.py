from __future__ import annotations

from typing import List, Optional

from .logger import SystemLogger
from .constants import BASIC_ROM_SIZE, KERNAL_ROM_SIZE, CHARGEN_ROM_SIZE
from .sid_chip import SidChip
from .sid_names import analyze_sid_write, get_sid_reg_name
from .cia6526 import Cia6526
from .vic_ii import VicII
from .sid_types import C64Config


def _u8(v: int) -> int:
    return v & 0xFF


def _u16(v: int) -> int:
    return v & 0xFFFF


class MemoryBank:
    """C64 memory map + I/O routing.

    Port of TS MemoryBank with fixes:
    - Correct open-bus/value persistence
    - SID read signature mismatch fixed
    """

    def __init__(self, cfg: Optional[C64Config] = None):
        self.cfg = cfg or C64Config()
        self.ram = bytearray(65536)
        # VIC color RAM (1024 bytes, 4-bit storage)
        self.color_ram = bytearray(1024)
        self.basic: Optional[bytes] = None
        self.kernal: Optional[bytes] = None
        self.chargen: Optional[bytes] = None

        self.ioEnabled = True
        self.basicEnabled = True
        self.kernalEnabled = True
        self.chargenEnabled = False

        # 6510 CPU I/O port ($0000 DDR, $0001 data). Real banking uses
        # DDR-masked outputs and pulled-high inputs.
        #
        # - $0000: DDR (1=output, 0=input)
        # - $0001: PORT DATA (when DDR bit=1)
        # - Effective read-back: (PORT & DDR) | (PORT_IN & ~DDR)
        # - Banking uses effective bits 0..2: LORAM/HIRAM/CHAREN.
        self._port_in = 0xFF  # default pulled-high inputs (tape/IEC/etc. not wired here)

        self.lastBusValue = 0
        self.busHold = 0
        self.busHoldDecay = int(self.cfg.busPersistenceCycles)

        self.sids: List[SidChip] = []
        self.sidBases: List[int] = []
        self.cia1 = Cia6526('CIA1')
        self.cia2 = Cia6526('CIA2')
        self.vic = VicII()

        # CPU context (best-effort). Cpu6502 may populate these before
        # invoking read/write, allowing logs to include cycle + PC.
        self._cpu_cycle: int = -1
        self._cpu_pc: int = -1

        SystemLogger.log('Memory', f"MemoryBank init: RAM=65536 bytes | busPersistenceCycles={self.cfg.busPersistenceCycles}", 'debug', category='memory')
        SystemLogger.log('Memory', f"Initial banking: IO={self.ioEnabled} BASIC={self.basicEnabled} KERNAL={self.kernalEnabled} CHARGEN={self.chargenEnabled}", 'debug', category='memory')

        # Initialize CPU port to common C64 post-reset defaults (what KERNAL does
        # very early on), so banking starts in a sane/realistic state even when
        # running PSID without real ROMs.
        # NOTE: these are RAM-mapped registers, so they live in self.ram.
        self.ram[0x0000] = 0x2F  # DDR
        self.ram[0x0001] = 0x37  # PORT
        self._update_banking_from_cpu_port('init')

    def _cpu_port_effective(self) -> int:
        ddr = self.ram[0x0000] & 0xFF
        port = self.ram[0x0001] & 0xFF
        return ((port & ddr) | (self._port_in & (~ddr & 0xFF))) & 0xFF

    def _update_banking_from_cpu_port(self, reason: str) -> None:
        """Update banking flags from the 6510 port ($0000/$0001) behavior.

        Real C64 banking uses DDR-masked bits. If a bit is configured as input,
        it reads high due to pull-ups.

        Bits:
          b0=LORAM, b1=HIRAM, b2=CHAREN
        """
        eff = self._cpu_port_effective()
        loram = (eff & 0x01) != 0
        hiram = (eff & 0x02) != 0
        charen = (eff & 0x04) != 0

        old = (self.ioEnabled, self.basicEnabled, self.kernalEnabled, self.chargenEnabled)

        # PLA-ish mapping (no cartridge/ultimax modeling here)
        self.ioEnabled = charen
        self.kernalEnabled = hiram
        self.basicEnabled = loram and hiram
        self.chargenEnabled = (not charen) and hiram

        # Enforce ROM visibility invariants
        self._sanitize_banking(f'cpu_port:{reason}')

        new = (self.ioEnabled, self.basicEnabled, self.kernalEnabled, self.chargenEnabled)
        if old != new and SystemLogger.category_enabled('memory'):
            SystemLogger.log(
                'Bank',
                f"CPU port banking ({reason}): DDR=${self.ram[0x0000]:02X} PORT=${self.ram[0x0001]:02X} EFF=${eff:02X} -> IO={self.ioEnabled} KERNAL={self.kernalEnabled} BASIC={self.basicEnabled} CHARGEN={self.chargenEnabled}",
                'debug',
                category='memory',
                fields={
                    'reason': reason,
                    'ddr': int(self.ram[0x0000]),
                    'port': int(self.ram[0x0001]),
                    'port_effective': int(eff),
                    'ioEnabled': self.ioEnabled,
                    'kernalEnabled': self.kernalEnabled,
                    'basicEnabled': self.basicEnabled,
                    'chargenEnabled': self.chargenEnabled,
                },
            )

    def classify_address(self, addr: int) -> dict:
        """Return a small classification dict for an address.

        Used by verbose CPU memory logging so we can label RAM/ROM/I/O targets.
        """
        a = _u16(addr)
        self._sanitize_banking('classify')
        info = {'addr': a, 'region': 'RAM'}

        # ROM ranges
        if 0xA000 <= a <= 0xBFFF and self.basicEnabled and self.basic is not None:
            info['region'] = 'BASIC_ROM'
            return info
        if 0xE000 <= a <= 0xFFFF and self.kernalEnabled and self.kernal is not None:
            info['region'] = 'KERNAL_ROM'
            return info

        # I/O or chargen
        if 0xD000 <= a <= 0xDFFF:
            if self.ioEnabled:
                # SID(s)
                for i, base in enumerate(self.sidBases):
                    if base <= a <= base + 0x1F:
                        info['region'] = 'SID'
                        info['sid_chip'] = i
                        info['sid_base'] = base
                        info['sid_reg'] = a - base
                        return info
                if 0xD000 <= a <= 0xD3FF:
                    info['region'] = 'VIC'
                    return info
                if 0xDC00 <= a <= 0xDCFF:
                    info['region'] = 'CIA1'
                    return info
                if 0xDD00 <= a <= 0xDDFF:
                    info['region'] = 'CIA2'
                    return info
                info['region'] = 'IO_OPEN_BUS'
                return info
            else:
                if self.chargenEnabled and self.chargen is not None:
                    info['region'] = 'CHARGEN_ROM'
                    return info
                info['region'] = 'IO_DISABLED_RAM_SHADOW'
                return info

        return info

    def _sanitize_banking(self, reason: str) -> None:
        """Enforce ROM visibility invariants.

        Rule: If banking says BASIC/KERNAL/CHARGEN is visible, the ROM bytes must exist.
        If ROM bytes are missing, we force the corresponding bank flag OFF and log it.

        This prevents the "bank says ROM, mapper falls through to RAM" correctness bug.
        """
        forced = []
        # BASIC
        if self.basicEnabled and self.basic is None:
            self.basicEnabled = False
            forced.append('BASIC')
        # KERNAL
        if self.kernalEnabled and self.kernal is None:
            self.kernalEnabled = False
            forced.append('KERNAL')
        # CHARGEN (only visible when IO disabled)
        if self.chargenEnabled and self.chargen is None:
            self.chargenEnabled = False
            forced.append('CHARGEN')

        if forced:
            SystemLogger.log(
                'Bank',
                f"Bank sanitize ({reason}): forced OFF missing ROM(s): {','.join(forced)} | IO={self.ioEnabled} BASIC={self.basicEnabled} KERNAL={self.kernalEnabled} CHARGEN={self.chargenEnabled}",
                'warn',
                category='memory',
                fields={
                    'reason': reason,
                    'forced_off': forced,
                    'ioEnabled': self.ioEnabled,
                    'basicEnabled': self.basicEnabled,
                    'kernalEnabled': self.kernalEnabled,
                    'chargenEnabled': self.chargenEnabled,
                },
            )

    def reset(self) -> None:
        SystemLogger.log('Memory', 'Resetting RAM + I/O devices', 'debug', category='boot')
        for i in range(len(self.ram)):
            self.ram[i] = 0

        # 6510 port default state after a typical C64 reset sequence.
        # Many tunes (even PSID) assume the common $0000/$0001 setup.
        self.ram[0x0000] = 0x2F  # DDR
        self.ram[0x0001] = 0x37  # PORT
        self._update_banking_from_cpu_port('reset')

        self.lastBusValue = 0
        self.busHold = 0
        self.cia1.reset()
        self.cia2.reset()
        self.vic.reset()
        for sid in self.sids:
            sid.reset()

    def install_roms(self, basic: Optional[bytes], kernal: Optional[bytes], chargen: Optional[bytes]) -> None:
        if basic is not None and len(basic) != BASIC_ROM_SIZE:
            SystemLogger.log('Memory', f'Warning: BASIC ROM should be {BASIC_ROM_SIZE} bytes, got {len(basic)}', 'warn')
        if kernal is not None and len(kernal) != KERNAL_ROM_SIZE:
            SystemLogger.log('Memory', f'Warning: KERNAL ROM should be {KERNAL_ROM_SIZE} bytes, got {len(kernal)}', 'warn')
        if chargen is not None and len(chargen) != CHARGEN_ROM_SIZE:
            SystemLogger.log('Memory', f'Warning: CHARGEN ROM should be {CHARGEN_ROM_SIZE} bytes, got {len(chargen)}', 'warn')

        self.basic = basic
        self.kernal = kernal
        self.chargen = chargen

        SystemLogger.log('Memory', f"ROM install: BASIC={'yes' if basic else 'no'}({len(basic) if basic else 0}) KERNAL={'yes' if kernal else 'no'}({len(kernal) if kernal else 0}) CHARGEN={'yes' if chargen else 'no'}({len(chargen) if chargen else 0})", 'debug', category='memory')

        # Enforce invariants immediately: visible banks require bytes
        self._sanitize_banking('install_roms')

    def install_sids(self, sids: List[SidChip], bases: List[int]) -> None:
        self.sids = sids
        self.sidBases = bases
        SystemLogger.log('Memory', f"SID map: count={len(sids)} bases={[hex(b) for b in bases]}", 'debug', category='boot')

    def poke(self, addr: int, val: int) -> None:
        self.write(addr, val)

    def decay_bus(self, cycles: int) -> None:
        if self.busHoldDecay <= 0:
            return
        self.busHold = max(0, self.busHold - max(0, int(cycles)))

    def _touch_bus(self, val: int) -> None:
        self.lastBusValue = _u8(val)
        self.busHold = self.busHoldDecay

    def _open_bus(self) -> int:
        if self.busHoldDecay <= 0:
            return self.lastBusValue
        return self.lastBusValue if self.busHold > 0 else 0xFF

    def read(self, addr: int) -> int:
        a = _u16(addr)
        # Special: 6510 port reads
        if a == 0x0000:
            val = int(self.ram[0x0000])
            self._touch_bus(val)
            return val
        if a == 0x0001:
            val = int(self._cpu_port_effective())
            self._touch_bus(val)
            return val

        self._sanitize_banking('read')

        # RAM or ROM
        if 0xA000 <= a <= 0xBFFF and self.basicEnabled and self.basic is not None:
            val = self.basic[a - 0xA000]
            self._touch_bus(val)
            return val

        if 0xE000 <= a <= 0xFFFF and self.kernalEnabled and self.kernal is not None:
            val = self.kernal[a - 0xE000]
            self._touch_bus(val)
            return val

        # I/O / chargen
        if 0xD000 <= a <= 0xDFFF:
            if self.ioEnabled:
                # SID(s)
                for i, base in enumerate(self.sidBases):
                    if a >= base and a <= base + 0x1F:
                        sidReg = a - base
                        val = self.sids[i].read(sidReg, self._open_bus())
                        self._touch_bus(val)
                        return val

                # SID #0 mirror ($D400-$D7FF): 32-byte register mirrors if no other SID base matched
                if (0xD400 <= a <= 0xD7FF) and (len(self.sids) > 0) and (len(self.sidBases) > 0) and (self.sidBases[0] == 0xD400):
                    sidReg = (a - 0xD400) & 0x1F
                    val = self.sids[0].read(sidReg, self._open_bus())
                    self._touch_bus(val)
                    return val


                # VIC
                if 0xD000 <= a <= 0xD3FF:
                    val = self.vic.read(a)
                    self._touch_bus(val)
                    return val

                # Color RAM ($D800-$DBFF): 4-bit storage with open-bus high nibble
                if 0xD800 <= a <= 0xDBFF:
                    idx = a - 0xD800
                    val = (self._open_bus() & 0xF0) | (self.color_ram[idx] & 0x0F)
                    self._touch_bus(val)
                    return val

                # CIA1
                if 0xDC00 <= a <= 0xDCFF:
                    val = self.cia1.read(a)
                    self._touch_bus(val)
                    return val

                # CIA2
                if 0xDD00 <= a <= 0xDDFF:
                    val = self.cia2.read(a)
                    self._touch_bus(val)
                    return val

                # Unmapped I/O -> open bus
                val = self._open_bus()
                self._touch_bus(val)
                return val

            # chargen when I/O disabled
            if self.chargenEnabled and self.chargen is not None:
                val = self.chargen[a - 0xD000]
                self._touch_bus(val)
                return val

        val = self.ram[a]
        self._touch_bus(val)
        return val

    def peek(self, addr: int) -> int:
        """Side-effect-free read used for introspection.

        This method **does not** touch the open-bus latch and **does not**
        invoke I/O device reads. It is intended for logic that needs to
        inspect upcoming opcodes (e.g. call()-depth tracking) without
        perturbing emulation.
        """
        a = _u16(addr)
        self._sanitize_banking('peek')

        if 0xA000 <= a <= 0xBFFF and self.basicEnabled and self.basic is not None:
            return self.basic[a - 0xA000]

        if 0xE000 <= a <= 0xFFFF and self.kernalEnabled and self.kernal is not None:
            return self.kernal[a - 0xE000]

        if 0xD000 <= a <= 0xDFFF:
            # Avoid I/O side effects; if I/O is disabled, chargen is visible.
            if (not self.ioEnabled) and self.chargenEnabled and self.chargen is not None:
                return self.chargen[a - 0xD000]
            # Otherwise return RAM shadow.
            return self.ram[a]

        return self.ram[a]

    def write(self, addr: int, val: int) -> None:
        a = _u16(addr)
        v = _u8(val)

        # 6510 port ($0000 DDR, $0001 data)
        if a == 0x0000:
            old = int(self.ram[0x0000])
            self.ram[0x0000] = v
            self._touch_bus(v)
            self._update_banking_from_cpu_port('$0000')
            if old != v:
                SystemLogger.log('Bank', f"$0000(DDR)={v:02X} (was {old:02X})", 'debug', category='memory')
            return

        if a == 0x0001:
            old = int(self.ram[0x0001])
            self.ram[0x0001] = v
            self._touch_bus(v)
            self._update_banking_from_cpu_port('$0001')
            if old != v:
                SystemLogger.log('Bank', f"$0001(PORT)={v:02X} (was {old:02X})", 'debug', category='memory')
            return

        # RAM always writable
        self.ram[a] = v

        # I/O writes
        if 0xD000 <= a <= 0xDFFF and self.ioEnabled:
            # SID(s)
            for i, base in enumerate(self.sidBases):
                if a >= base and a <= base + 0x1F:
                    reg = a - base                    # NOTE: SIDREG/D418/SIDFLT/DIGI logging is owned by Cpu6502.write() (single source of truth)
                    # to avoid duplicate forensic events. We only log here for *host* writes (no CPU cycle context).
                    cyc = int(getattr(self, '_cpu_cycle', -1))
                    pc = int(getattr(self, '_cpu_pc', -1))
                    if cyc < 0 and (SystemLogger.category_enabled('sidregs') or SystemLogger.category_enabled('sidvol') or SystemLogger.category_enabled('sidfilter') or SystemLogger.category_enabled('digi')):
                        rn = get_sid_reg_name(reg)
                        clk = int(getattr(self.sids[i], 'clock_hz', 985_248))
                        ann = analyze_sid_write(reg, v, clock_freq=clk, cycle=None)
                        SystemLogger.log(
                            'SIDREG',
                            f"HOST pc=${pc:04X} chip={i} @{a:04X} reg=0x{reg:02X} {rn} <- ${v:02X} | {ann}",
                            'debug',
                            category='sidregs',
                            fields={
                                'cycle': cyc,
                                'pc': pc,
                                'pc_hex': (f"0x{pc:04X}" if pc >= 0 else None),
                                'chip': i,
                                'addr': a,
                                'addr_hex': f"0x{a:04X}",
                                'reg': reg,
                                'reg_index': reg,
                                'reg_hex': f"0x{reg:02X}",
                                'name': rn,
                                'value': v,
                                'value_hex': f"0x{v:02X}",
                                'annotation': ann,
                                'host_write': True,
                            },
                        )

                    self.sids[i].write(reg, v)
                    self._touch_bus(v)
                    return

            # SID #0 mirror ($D400-$D7FF): 32-byte register mirrors if no other SID base matched
            if (0xD400 <= a <= 0xD7FF) and (len(self.sids) > 0) and (len(self.sidBases) > 0) and (self.sidBases[0] == 0xD400):
                sidReg = (a - 0xD400) & 0x1F
                self.sids[0].write(sidReg, v)
                self._touch_bus(v)
                return

            # VIC
            if 0xD000 <= a <= 0xD3FF:
                self.vic.write(a, v)
                self._touch_bus(v)
                return

            # Color RAM: $D800-$DBFF (4-bit)
            if 0xD800 <= a <= 0xDBFF:
                idx = a - 0xD800
                self.color_ram[idx] = v & 0x0F
                self._touch_bus(v)
                return

            # CIA1
            if 0xDC00 <= a <= 0xDCFF:
                self.cia1.write(a, v)
                self._touch_bus(v)
                return

            # CIA2
            if 0xDD00 <= a <= 0xDDFF:
                self.cia2.write(a, v)
                self._touch_bus(v)
                return

        self._touch_bus(v)

    def load(self, addr: int, data: bytes) -> None:
        a = _u16(addr)
        end = min(65536, a + len(data))
        self.ram[a:end] = data[: end - a]
        SystemLogger.log('Memory', f'Loaded {end-a} bytes at ${a:04X}', 'debug', category='memory')
