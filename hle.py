from __future__ import annotations

from .logger import SystemLogger


class HLE:
    """High Level Emulation / Hardware Vector Layer.

    Mirrors the TS generator. Produces 8 KiB KERNAL + BASIC stubs.
    """

    @staticmethod
    def generate_kernal() -> bytes:
        SystemLogger.log('HLE', 'Synthesizing KERNAL HVL (8 KiB) with reset/IRQ/NMI vectors + minimal ISR chain', 'debug', category='boot')
        rom = bytearray([0xEA] * 8192)  # NOP

        # Hardware vectors at $FFFA-$FFFF map to offsets 0x1FFA-0x1FFF within 8K at $E000
        rom[0x1FFA] = 0x43
        rom[0x1FFB] = 0xFE
        rom[0x1FFC] = 0x00
        rom[0x1FFD] = 0xE0
        rom[0x1FFE] = 0x48
        rom[0x1FFF] = 0xFF

        # Reset stub at $E000 (offset 0)
        p = 0
        rom[p:p+2] = bytes([0x78, 0xD8])  # SEI CLD
        p += 2
        rom[p:p+2] = bytes([0xA2, 0xFF]); p += 2  # LDX #$FF
        rom[p] = 0x9A; p += 1  # TXS
        rom[p:p+2] = bytes([0xA9, 0x2F]); p += 2
        rom[p:p+2] = bytes([0x85, 0x00]); p += 2
        rom[p:p+2] = bytes([0xA9, 0x37]); p += 2
        rom[p:p+2] = bytes([0x85, 0x01]); p += 2
        rom[p:p+3] = bytes([0x20, 0x8D, 0xFF]); p += 3  # JSR $FF8D
        rom[p] = 0x58; p += 1  # CLI
        rom[p] = 0x60; p += 1  # RTS

        # ISR entry $FF48 (offset 0x1F48)
        ff48 = 0x1F48
        rom[ff48:ff48+8] = bytes([
            0x48,  # PHA
            0x8A,  # TXA
            0x48,  # PHA
            0x98,  # TYA
            0x48,  # PHA
            0x6C, 0x14, 0x03,  # JMP ($0314)
        ])

        # NMI entry $FE43 (offset 0x1E43)
        fe43 = 0x1E43
        rom[fe43:fe43+3] = bytes([0x6C, 0x18, 0x03])

        # Default IRQ handler $EA31 (offset $EA31-$E000 = 0x0A31)
        ea31 = 0x0A31
        rom[ea31:ea31+9] = bytes([
            0xAD, 0x0D, 0xDC,  # LDA $DC0D
            0x68, 0xA8,        # PLA; TAY
            0x68, 0xAA,        # PLA; TAX
            0x68,              # PLA
            0x40,              # RTI
        ])

        # NMI ack $FE47
        fe47 = 0x1E47
        rom[fe47:fe47+4] = bytes([0xAD, 0x0D, 0xDD, 0x40])

        # BRK fallback $FE66
        fe66 = 0x1E66
        rom[fe66] = 0x40

        # Vector table $FD30 (offset 0x1D30)
        table_values = [
            0x31, 0xEA, 0x66, 0xFE, 0x47, 0xFE, 0x4A, 0xF3,
            0x91, 0xF2, 0x0E, 0xF2, 0x50, 0xF2, 0x33, 0xF3,
            0x57, 0xF1, 0xCA, 0xF1, 0xED, 0xF6, 0x3E, 0xF1,
            0x2F, 0xF3, 0x66, 0xFE, 0xA5, 0xF4, 0xED, 0xF5,
        ]
        rom[0x1D30:0x1D30+len(table_values)] = bytes(table_values)

        def set_stub(addr: int, code: list[int]) -> None:
            off = addr - 0xE000
            rom[off:off+len(code)] = bytes(code)

        # KERNAL jump table stubs
        set_stub(0xFF81, [0x60])
        set_stub(0xFF84, [0xA2, 0x00, 0x9A, 0x60])
        set_stub(0xFF87, [0xA9, 0x00, 0x60])

        # RESTOR $FF8D
        set_stub(0xFF8D, [
            0xA2, 0x1F,
            0xBD, 0x30, 0xFD,
            0x9D, 0x14, 0x03,
            0xCA,
            0x10, 0xF7,
            0x60,
        ])

        return bytes(rom)

    @staticmethod
    def generate_basic() -> bytes:
        SystemLogger.log('HLE', 'Synthesizing BASIC HVL (8 KiB) header stub', 'debug', category='boot')
        rom = bytearray([0xEA] * 8192)
        rom[0] = 0x94
        rom[1] = 0xE3
        rom[2] = 0x7B
        rom[3] = 0xE3
        return bytes(rom)
