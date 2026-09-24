from __future__ import annotations

import math
from typing import List, Optional

SID_REG_NAMES: List[str] = [
    'V1_FREQ_LO', 'V1_FREQ_HI', 'V1_PULSE_LO', 'V1_PULSE_HI', 'V1_CTRL', 'V1_ATK_DECY', 'V1_STN_RELS',
    'V2_FREQ_LO', 'V2_FREQ_HI', 'V2_PULSE_LO', 'V2_PULSE_HI', 'V2_CTRL', 'V2_ATK_DECY', 'V2_STN_RELS',
    'V3_FREQ_LO', 'V3_FREQ_HI', 'V3_PULSE_LO', 'V3_PULSE_HI', 'V3_CTRL', 'V3_ATK_DECY', 'V3_STN_RELS',
    'FLTR_CUT_LO', 'FLTR_CUT_HI', 'FLTR_RESO_RTE', 'MASTER_VOL_DIGI',
    'POT_X', 'POT_Y', 'OSC3_RAND', 'ENV3'
]

NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]
_reg_cache = bytearray(32)

# Extra state for "digi" detection on $D418 volume DAC.
_last_d418_val: int = 0
_last_d418_cycle: Optional[int] = None
_last_vol: int = 0
_last_vol_cycle: Optional[int] = None


def get_sid_reg_name(reg: int) -> str:
    r = reg & 0x1F
    if r >= len(SID_REG_NAMES):
        return f"MIRROR_{r:02X}"
    return SID_REG_NAMES[r]


def analyze_sid_write(reg: int, val: int, clock_freq: int = 985_248, *, cycle: Optional[int] = None) -> str:
    r = reg & 0x1F
    v = val & 0xFF
    _reg_cache[r] = v

    if (r % 7 == 4) and (r < 21):
        wf = []
        if v & 0x10: wf.append('TRI')
        if v & 0x20: wf.append('SAW')
        if v & 0x40: wf.append('PUL')
        if v & 0x80: wf.append('NOI')
        flags = []
        if v & 0x01: flags.append('GATE')
        if v & 0x02: flags.append('SYNC')
        if v & 0x04: flags.append('RING')
        if v & 0x08: flags.append('TEST')
        return f"WF:[{'+'.join(wf) if wf else 'OFF'}] {('|'.join(flags) if flags else 'IDLE')}"

    if (r % 7 == 5) and (r < 21):
        atk_tab = [2, 8, 16, 24, 38, 56, 68, 80, 100, 250, 500, 800, 1000, 3000, 5000, 8000]
        dcy_tab = [6, 24, 48, 72, 114, 168, 204, 240, 300, 750, 1500, 2400, 3000, 9000, 15000, 24000]
        atk = atk_tab[v >> 4]
        dcy = dcy_tab[v & 0x0F]
        return f"A:{atk}ms D:{dcy}ms"

    if (r % 7 == 6) and (r < 21):
        rel_tab = [6, 24, 48, 72, 114, 168, 204, 240, 300, 750, 1500, 2400, 3000, 9000, 15000, 24000]
        rel = rel_tab[v & 0x0F]
        sus = (v >> 4) / 15 * 100
        return f"S:{sus:.0f}% R:{rel}ms"

    if r in (0, 1, 7, 8, 14, 15):
        base = (r // 7) * 7
        full_freq = _reg_cache[base] | (_reg_cache[base + 1] << 8)
        hz = (full_freq * clock_freq) / 16777216
        if hz < 1:
            return f"FREQ: ${full_freq:04X} (DC)"
        midi = 69 + 12 * math.log2(hz / 440.0)
        note_idx = int(round(midi)) % 12
        octv = int(math.floor(round(midi) / 12) - 1)
        return f"{hz:.1f}Hz [{NOTE_NAMES[note_idx]}{octv}]"

    if r in (2, 3, 9, 10, 16, 17):
        base = (r // 7) * 7 + 2
        full_pw = _reg_cache[base] | ((_reg_cache[base + 1] & 0x0F) << 8)
        duty = full_pw / 40.95
        return f"PW: {duty:.1f}%"

    if r in (21, 22):
        full_cut = (_reg_cache[21] & 7) | (_reg_cache[22] << 3)
        hz = 30 + (full_cut * 5.8)
        return f"FC: ~{int(round(hz))}Hz"

    if r == 23:
        routes = []
        if v & 0x01: routes.append('V1')
        if v & 0x02: routes.append('V2')
        if v & 0x04: routes.append('V3')
        if v & 0x08: routes.append('EXT')
        return f"RES:{v >> 4} RT:[{'|'.join(routes) if routes else 'BYP'}]"

    if r == 24:
        # $D418 MASTER VOL + filter mode + voice3 off
        global _last_d418_val, _last_d418_cycle, _last_vol, _last_vol_cycle

        vol = v & 0x0F
        modes = []
        if v & 0x10: modes.append('LP')
        if v & 0x20: modes.append('BP')
        if v & 0x40: modes.append('HP')
        if v & 0x80: modes.append('3OFF')

        parts = []

        # Volume change / possible digi DAC usage
        if vol != _last_vol:
            if cycle is not None and _last_vol_cycle is not None:
                dt = int(cycle) - int(_last_vol_cycle)
                if dt > 0 and clock_freq > 0:
                    # Approx write-rate in Hz
                    hz = float(clock_freq) / float(dt)
                    # Typical digi playback updates are in the ~3-10kHz range.
                    # Typical digi playback updates are in the ~3-10kHz range. We flag
                    # anything above 2kHz as "DIGI" to highlight likely DAC sample streams.
                    digi_flag = ' DIGI' if hz >= 2000.0 else ''
                    parts.append(f"VOL:{_last_vol}->{vol} (\u0394={vol-_last_vol:+d}, {hz:,.0f}Hz){digi_flag}")
                else:
                    parts.append(f"VOL:{_last_vol}->{vol} (\u0394={vol-_last_vol:+d})")
            else:
                parts.append(f"VOL:{_last_vol}->{vol} (\u0394={vol-_last_vol:+d})")
            _last_vol = vol
            _last_vol_cycle = int(cycle) if cycle is not None else _last_vol_cycle
        else:
            parts.append(f"VOL:{vol}")

        # Filter mode bits
        parts.append(f"FILT:[{'+'.join(modes) if modes else 'BYP'}]")

        # Raw for completeness
        if cycle is not None and _last_d418_cycle is not None:
            dt2 = int(cycle) - int(_last_d418_cycle)
            parts.append(f"dt={dt2}cy")
        _last_d418_val = v
        _last_d418_cycle = int(cycle) if cycle is not None else _last_d418_cycle

        return ' '.join(parts)

    return f"${v:02X}"
