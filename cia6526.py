from __future__ import annotations

"""MOS 6526 CIA (timers, TOD, serial, ports).

This project originally carried a simplified CIA with only Timer A/Timer B
phi2 / underflow-A modes.

This version upgrades correctness and compatibility for real-world C64
drivers (including many RSID-ish tunes) by implementing:
  - Timer B input modes 0/1/2/3 (phi2, CNT, underflow-A, underflow-A+CNT)
  - TOD clock + TOD alarm (ICR bit 0x04)
  - Serial shift register (SDR @ $0C) + CNT/SP pin modeling + ICR bit 0x08
  - Latching semantics for TOD reads (latch on hours read, unlatch on tenths read)

It is still a *model* (not a full board-level wiring simulator): keyboard/
joystick/IEC/tape/userport lines are not wired, but the port and pin
interfaces exist so they can be hooked up later.
"""

from .logger import SystemLogger


def _u8(v: int) -> int:
    return v & 0xFF


def _u16(v: int) -> int:
    return v & 0xFFFF


def _bcd_to_int(b: int) -> int:
    return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)


def _int_to_bcd(n: int) -> int:
    n = max(0, int(n))
    return ((_u8(n // 10) & 0x0F) << 4) | (_u8(n % 10) & 0x0F)


class Cia6526:
    """MOS 6526 CIA.

    Register map (low nybble):
      00 PRA   01 PRB   02 DDRA  03 DDRB
      04 TA LO 05 TA HI 06 TB LO 07 TB HI
      08 TOD 1/10 09 TOD SEC 0A TOD MIN 0B TOD HR
      0C SDR   0D ICR   0E CRA   0F CRB
    """

    # ICR bits
    ICR_TA = 0x01
    ICR_TB = 0x02
    ICR_ALARM = 0x04
    ICR_SERIAL = 0x08
    ICR_FLAG = 0x10  # /FLAG pin (not wired here)

    def __init__(self, name: str):
        self.name = name

        # Ports + DDR
        self.pra = 0xFF
        self.prb = 0xFF
        self.ddra = 0x00
        self.ddrb = 0x00
        self.port_in_a = 0xFF
        self.port_in_b = 0xFF

        # Timers
        self.latchA = 0xFFFF
        self.latchB = 0xFFFF
        self.timerA = 0xFFFF
        self.timerB = 0xFFFF

        # Interrupt control
        self.icr = 0x00
        self.icrMask = 0x00
        self.cra = 0x00
        self.crb = 0x00
        self.irqLine = False

        # CNT/SP pins (serial + Timer B CNT modes)
        self.cnt_level = 1
        self.sp_level = 1
        self._cnt_pulse = False

        # Serial shift register
        self.sdr = 0x00
        self._serial_active = False
        self._serial_bits = 0

        # TOD clock + alarm (BCD-encoded registers)
        self._tod_running = True
        self._tod_latched = False
        self._tod_latch = [0x00, 0x00, 0x00, 0x00]
        self._tod = [0x00, 0x00, 0x00, 0x00]  # 1/10, sec, min, hr
        self._alarm = [0x00, 0x00, 0x00, 0x00]
        self._tod_write_hold = False

        # Timing for TOD pulses (derived from clock)
        self.clock_hz = 985_248
        self._tod_acc = 0.0
        self._tod_cycles_per_pulse = self.clock_hz / 60.0

    # ---------------------------------------------------------------------
    # External wiring hooks
    # ---------------------------------------------------------------------

    def set_clock_hz(self, clock_hz: int) -> None:
        """Set phi2 clock rate used for TOD pulse scheduling."""
        self.clock_hz = max(1, int(clock_hz))
        self._recalc_tod_rate()

    def set_port_inputs(self, a: int | None = None, b: int | None = None) -> None:
        if a is not None:
            self.port_in_a = _u8(a)
        if b is not None:
            self.port_in_b = _u8(b)

    def set_sp(self, level: int) -> None:
        self.sp_level = 1 if (level & 1) else 0

    def set_cnt(self, level: int) -> None:
        level = 1 if (level & 1) else 0
        # rising edge generates a pulse
        if self.cnt_level == 0 and level == 1:
            self._cnt_pulse = True
        self.cnt_level = level

    def pulse_cnt(self) -> None:
        """Convenience: generate one CNT rising-edge pulse."""
        self._cnt_pulse = True

    # ---------------------------------------------------------------------
    # Core
    # ---------------------------------------------------------------------

    def reset(self) -> None:
        self.pra = 0xFF
        self.prb = 0xFF
        self.ddra = 0x00
        self.ddrb = 0x00
        self.port_in_a = 0xFF
        self.port_in_b = 0xFF

        self.latchA = 0xFFFF
        self.latchB = 0xFFFF
        self.timerA = 0xFFFF
        self.timerB = 0xFFFF

        self.icr = 0x00
        self.icrMask = 0x00
        self.cra = 0x00
        self.crb = 0x00
        self.irqLine = False

        self.cnt_level = 1
        self.sp_level = 1
        self._cnt_pulse = False

        self.sdr = 0x00
        self._serial_active = False
        self._serial_bits = 0

        self._tod_running = True
        self._tod_latched = False
        self._tod_write_hold = False
        self._tod_latch = [0x00, 0x00, 0x00, 0x00]
        self._tod = [0x00, 0x00, 0x00, 0x00]
        self._alarm = [0x00, 0x00, 0x00, 0x00]
        self._tod_acc = 0.0
        self._recalc_tod_rate()

        SystemLogger.log('CIA', f'[{self.name}] Hardware Reset.', 'debug', category='cia')

    def peek_icr(self) -> int:
        return self.icr & 0x1F

    def _recalc_tod_rate(self) -> None:
        # CRA bit7 selects TOD rate: 0=60Hz, 1=50Hz (common convention)
        tod_hz = 50.0 if (self.cra & 0x80) else 60.0
        self._tod_cycles_per_pulse = float(self.clock_hz) / max(1.0, tod_hz)

    def step(self, cycles: int) -> None:
        """Advance CIA by `cycles` phi2 cycles."""
        c = max(0, int(cycles))
        for _ in range(c):
            underA = self._tick_timer_a()
            self._tick_timer_b(underA)
            self._tick_tod(1)
            self._cnt_pulse = False  # consume at most one pulse per phi2
        self._update_irq_line()

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _tick_timer_a(self) -> bool:
        if (self.cra & 0x01) == 0:
            return False
        self.timerA = _u16(self.timerA - 1)
        if self.timerA == 0xFFFF:
            self.icr |= self.ICR_TA
            self.timerA = self.latchA & 0xFFFF
            if (self.cra & 0x08) != 0:
                self.cra &= ~0x01
            return True
        return False

    def _tick_timer_b(self, underflowA: bool) -> None:
        if (self.crb & 0x01) == 0:
            return

        input_mode = (self.crb >> 5) & 0x03
        tickB = False

        if input_mode == 0:
            # phi2
            tickB = True
        elif input_mode == 1:
            # CNT pulses
            tickB = bool(self._cnt_pulse)
        elif input_mode == 2:
            # Timer A underflow
            tickB = bool(underflowA)
        elif input_mode == 3:
            # Timer A underflow *and* CNT pulse
            tickB = bool(underflowA and self._cnt_pulse)

        if not tickB:
            return

        self.timerB = _u16(self.timerB - 1)
        if self.timerB == 0xFFFF:
            self.icr |= self.ICR_TB
            self.timerB = self.latchB & 0xFFFF
            if (self.crb & 0x08) != 0:
                self.crb &= ~0x01

    # ------------------------------------------------------------------
    # TOD clock
    # ------------------------------------------------------------------

    def _tod_match_alarm(self) -> bool:
        return (self._tod[0] & 0x0F) == (self._alarm[0] & 0x0F) and self._tod[1:] == self._alarm[1:]

    def _tick_tod(self, phi2_cycles: int) -> None:
        if not self._tod_running:
            return
        self._tod_acc += float(phi2_cycles)
        while self._tod_acc >= self._tod_cycles_per_pulse:
            self._tod_acc -= self._tod_cycles_per_pulse
            self._tod_pulse()

    def _tod_pulse(self) -> None:
        """One TOD input pulse (50/60Hz). We accumulate 5/6 pulses per 1/10s."""
        # Convert to tenths via divider: 50Hz => 5 pulses per tenth, 60Hz => 6.
        pulses_per_tenth = 5 if (self.cra & 0x80) else 6
        div = getattr(self, '_tod_div', 0)
        div += 1
        if div < pulses_per_tenth:
            setattr(self, '_tod_div', div)
            return
        setattr(self, '_tod_div', 0)

        # Increment BCD time
        tenth = _bcd_to_int(self._tod[0] & 0x0F) + 1
        sec = _bcd_to_int(self._tod[1])
        minute = _bcd_to_int(self._tod[2])

        hr_raw = self._tod[3]
        pm = (hr_raw & 0x80) != 0
        hour = _bcd_to_int(hr_raw & 0x1F)
        if hour <= 0:
            hour = 12

        if tenth >= 10:
            tenth = 0
            sec += 1
            if sec >= 60:
                sec = 0
                minute += 1
                if minute >= 60:
                    minute = 0
                    hour += 1
                    if hour == 12:
                        pm = not pm
                    if hour > 12:
                        hour = 1

        self._tod[0] = (self._tod[0] & 0xF0) | (tenth & 0x0F)
        self._tod[1] = _int_to_bcd(sec)
        self._tod[2] = _int_to_bcd(minute)
        self._tod[3] = (_int_to_bcd(hour) & 0x1F) | (0x80 if pm else 0x00)

        if self._tod_match_alarm():
            self.icr |= self.ICR_ALARM

    # ------------------------------------------------------------------
    # Serial shift
    # ------------------------------------------------------------------

    def _serial_dir_output(self) -> bool:
        # CRA bit6: serial direction. 1=output, 0=input
        return (self.cra & 0x40) != 0

    def _serial_tick_on_cnt(self) -> None:
        if not self._serial_active:
            return

        if self._serial_dir_output():
            # Shift out MSB first onto SP
            bit = 1 if (self.sdr & 0x80) else 0
            self.sp_level = bit
            self.sdr = _u8((self.sdr << 1) & 0xFF)
        else:
            # Shift in from SP, MSB first
            self.sdr = _u8((self.sdr << 1) | (1 if self.sp_level else 0))

        self._serial_bits += 1
        if self._serial_bits >= 8:
            self._serial_active = False
            self._serial_bits = 0
            self.icr |= self.ICR_SERIAL

    # ------------------------------------------------------------------
    # IRQ line
    # ------------------------------------------------------------------

    def _update_irq_line(self) -> None:
        active = ((self.icr & self.icrMask) & 0x1F) != 0
        self.irqLine = bool(active)

    # ------------------------------------------------------------------
    # Bus access
    # ------------------------------------------------------------------

    def read(self, addr: int) -> int:
        reg = addr & 0x0F

        if reg == 0x0D:
            flags = self.icr & 0x1F
            master = 0x80 if ((flags & (self.icrMask & 0x1F)) != 0) else 0
            val = flags | master
            # Reading ICR clears it and deasserts IRQ.
            self.icr = 0
            self.irqLine = False
            return val & 0xFF

        if reg == 0x00:
            return (self.pra & self.ddra) | (self.port_in_a & (~self.ddra & 0xFF))
        if reg == 0x01:
            return (self.prb & self.ddrb) | (self.port_in_b & (~self.ddrb & 0xFF))
        if reg == 0x02:
            return self.ddra
        if reg == 0x03:
            return self.ddrb
        if reg == 0x04:
            return self.timerA & 0xFF
        if reg == 0x05:
            return (self.timerA >> 8) & 0xFF
        if reg == 0x06:
            return self.timerB & 0xFF
        if reg == 0x07:
            return (self.timerB >> 8) & 0xFF

        # TOD registers with latch behavior
        if reg in (0x08, 0x09, 0x0A, 0x0B):
            if reg == 0x0B:
                # Latch on hours read
                self._tod_latch = list(self._tod)
                self._tod_latched = True
            src = self._tod_latch if self._tod_latched else self._tod
            idx = reg - 0x08
            val = src[idx] & 0xFF
            if reg == 0x08:
                # Unlatch on tenths read
                self._tod_latched = False
            return val

        if reg == 0x0C:
            return self.sdr & 0xFF

        if reg == 0x0E:
            return self.cra & 0xFF
        if reg == 0x0F:
            return self.crb & 0xFF

        return 0xFF

    def write(self, addr: int, val: int) -> None:
        reg = addr & 0x0F
        v = _u8(val)

        if reg == 0x0D:
            # ICR mask
            if v & 0x80:
                self.icrMask |= (v & 0x1F)
            else:
                self.icrMask &= ~(v & 0x1F)
            self._update_irq_line()
            return

        # Timer latches
        if reg == 0x04:
            self.latchA = (self.latchA & 0xFF00) | v
            return
        if reg == 0x05:
            self.latchA = (self.latchA & 0x00FF) | (v << 8)
            if (self.cra & 0x01) == 0:
                self.timerA = self.latchA & 0xFFFF
            return
        if reg == 0x06:
            self.latchB = (self.latchB & 0xFF00) | v
            return
        if reg == 0x07:
            self.latchB = (self.latchB & 0x00FF) | (v << 8)
            if (self.crb & 0x01) == 0:
                self.timerB = self.latchB & 0xFFFF
            return

        # TOD / alarm
        if reg in (0x08, 0x09, 0x0A, 0x0B):
            idx = reg - 0x08
            set_alarm = (self.crb & 0x80) != 0
            target = self._alarm if set_alarm else self._tod

            # Writing TOD halts until tenths written (approx 6526 behavior)
            if not set_alarm:
                self._tod_running = False
                self._tod_write_hold = True

            # Hours: preserve PM bit on write, accept BCD for 1..12
            if reg == 0x0B:
                target[idx] = (v & 0x9F)  # keep PM (bit7), ignore unused bits
            elif reg == 0x08:
                target[idx] = (v & 0x0F) | (target[idx] & 0xF0)
            else:
                target[idx] = v

            # Release hold on tenths write
            if not set_alarm and reg == 0x08:
                self._tod_running = True
                self._tod_write_hold = False
            return

        if reg == 0x0C:
            # Load shift register and (re)arm serial transfer.
            self.sdr = v
            self._serial_active = True
            self._serial_bits = 0
            return

        if reg == 0x0E:
            load = (v & 0x10) != 0
            self.cra = v & ~0x10
            if load:
                self.timerA = self.latchA & 0xFFFF
            self._recalc_tod_rate()
            self._update_irq_line()
            return

        if reg == 0x0F:
            load = (v & 0x10) != 0
            self.crb = v & ~0x10
            if load:
                self.timerB = self.latchB & 0xFFFF
            self._update_irq_line()
            return

        # Ports
        if reg == 0x00:
            self.pra = v
        elif reg == 0x01:
            self.prb = v
        elif reg == 0x02:
            self.ddra = v
        elif reg == 0x03:
            self.ddrb = v

        # If CNT pulse occurs, also clock serial.
        if self._cnt_pulse:
            self._serial_tick_on_cnt()

        self._update_irq_line()
