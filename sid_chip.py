from __future__ import annotations

from typing import Any, Dict, List, Optional

from .logger import SystemLogger
from .sid_types import C64Config, SidModel


def _u8(v: int) -> int:
    return v & 0xFF


def _u24(v: int) -> int:
    return v & 0xFFFFFF


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v

# SID sync/ring source mapping (voice chain: 3→1→2→3).
# Voices here are 0-based: 0=Voice1, 1=Voice2, 2=Voice3.
def _sid_chain_source(dest_voice: int) -> int:
    return (dest_voice - 1) % 3  # 0<-2, 1<-0, 2<-1



class _ResIDWaveLUT:
    """Loader for reSID combined-waveform lookup tables.

    The provided .dat tables are expected to be 4096 bytes each (one per 12-bit phase index),
    with values in [0..127]. We scale them to [0..4095] (12-bit DAC domain) so the rest of
    the mixer can stay consistent.
    """

    _CACHE: Dict[str, Dict[int, List[int]]] = {}

    # Keyed by combined waveform mask on the (TRI|SAW|PULSE) subset
    # using SID CTRL waveform bits: TRI=0x10, SAW=0x20, PULSE=0x40
    _MASK_TO_SUFFIX = {
        # NOTE: filenames are "wave{model}__ST.dat" (double underscore) for TRI+SAW.
        # We already insert one underscore in the prefix ("wave{model}_"), so the suffix here is "_ST".
        0x10 | 0x20: "_ST",   # TRI+SAW
        0x10 | 0x40: "P_T",   # TRI+PULSE
        0x20 | 0x40: "PS_",   # SAW+PULSE
        0x10 | 0x20 | 0x40: "PST",  # TRI+SAW+PULSE
    }

    @classmethod
    def _load_table(cls, filename: str) -> List[int]:
        from importlib import resources

        try:
            data = resources.files('c64sid.sid.resid_lut').joinpath(filename).read_bytes()
        except Exception:
            return []

        if len(data) != 4096:
            return []

        # Scale 0..127 to 0..4095
        out = [0] * 4096
        for i, b in enumerate(data):
            out[i] = int((int(b) * 4095) / 127) if b else 0
        return out

    @classmethod
    def get(cls, model: SidModel, combo_mask: int) -> Optional[List[int]]:
        # Normalize model
        if model not in ('6581', '8580'):
            return None

        suffix = cls._MASK_TO_SUFFIX.get(combo_mask)
        if not suffix:
            return None

        key = str(model)
        bank = cls._CACHE.get(key)
        if bank is None:
            bank = {}
            cls._CACHE[key] = bank

        if combo_mask in bank:
            return bank[combo_mask] or None

        fname = f"wave{model}_{suffix}.dat"
        tab = cls._load_table(fname)
        bank[combo_mask] = tab
        if tab:
            SystemLogger.log('SID', f"Loaded reSID LUT: {fname} ({len(tab)} entries)", 'debug', category='boot')
        else:
            SystemLogger.log('SID', f"Failed to load reSID LUT: {fname} (missing/invalid)", 'warn', category='boot')
        return tab or None


class SidChip:
    """Deterministic SID register model + improved audio fidelity.

    This implementation focuses on:
      - Forensic determinism (stable results)
      - reSID LUT-based combined waveforms (6581/8580)
      - Envelope generator quirks: ADSR delay + rate-counter wrap behavior
      - D418 digi: RC low-pass + AC-coupled output path

    It is still not a full analog-perfect SID, but it is *much* closer for
    combined-wave and digi-heavy tunes.
    """

    # Envelope rate periods in SID clock cycles (reSID-style periods)
    # These tables are commonly used in emulation and align well for timing.
    _RATE_PERIODS = [
        9, 32, 63, 95, 149, 220, 267, 313,
        392, 977, 1954, 3126, 3907, 11720, 19532, 31251
    ]

    # Level thresholds for exponential counter period changes (reSID-like)
    # Produces the characteristic long decay tail.
    _EXP_THRESH = [0xFF, 0x5D, 0x36, 0x1A, 0x0E, 0x06, 0x00]
    _EXP_PERIOD = [1, 2, 4, 8, 16, 30]  # periods for ranges between thresholds

    def __init__(self, clock_hz: int, cfg: Optional[C64Config] = None):
        self.regs = bytearray(0x20)
        self.model: SidModel = 'UNKNOWN'
        self.clock_hz = int(clock_hz)
        self.cfg = cfg or C64Config()

        # Voice state
        self.phase = [0, 0, 0]       # 24-bit
        self.prev_phase = [0, 0, 0]  # for wrap detection

        # Envelope (internal counter + (optional) one-cycle output pipeline)
        self.env_ctr = [0, 0, 0]         # 0..255 (internal)
        self.env_out = [0, 0, 0]         # 0..255 (output)
        self.env_state = ['R', 'R', 'R'] # 'A'|'D'|'R'
        # reSID-style hold-zero latch: once the envelope reaches 0 in release it can "stick"
        # until a new gate-on transition clears it.
        self.hold_zero = [False, False, False]
        self.gate = [False, False, False]
        self._gate_pending = [None, None, None]  # type: ignore
        self._gate_delay = [0, 0, 0]             # 1-cycle gate latch delay

        # ADSR counters (rate counter & exponential counter)
        # Note: rate_counter triggers on equality to rate_period (wrap bug preserved).
        self.rate_ctr = [0, 0, 0]       # 15-bit
        self.exp_ctr = [0, 0, 0]
        self.exp_period = [1, 1, 1]

        # Simple noise LFSR per voice
        self._noise_seed = int(getattr(self.cfg, 'noiseSeed', 0x7FFFFF)) & 0x7FFFFF
        seed = self._noise_seed
        self.noise = [0x7FFFFF, 0x7FFFFF, seed if seed != 0 else 0x7FFFFF]

        # Filter state
        self.filter_lp = 0.0
        self.filter_bp = 0.0
        self.filter_hp = 0.0

        # 4-stage ladder filter state (audio-rate, topology-inspired).
        self._ladder = [0.0, 0.0, 0.0, 0.0]  # y1..y4
        self._ladder_last = 0.0
        self._last_filter_regs = (-1, -1, -1)  # (cutoff, res, mode)
        self._last_vol = -1



        self.osc3 = 0
        self.env3 = 0

        # D418 digi path (volume DAC + board RC network + AC coupling).
        # Defaults follow common board differences:
        #  - 6581-era boards often have large caps ("muffled", strong bass roll-off behavior)
        #  - 8580-era boards use much smaller caps (brighter digi response).
        self.sample_rate = int(getattr(self.cfg, 'sampleRate', 44100))
        self._d418_lp = 0.0
        self._d418_hp = 0.0
        self._d418_lp_prev = 0.0
        # Component assumptions (override via cfg if desired).
        roh = getattr(self.cfg, 'sidD418Rohm', None)
        cap_lp = getattr(self.cfg, 'sidD418CapLP_F', None)
        cap_hp = getattr(self.cfg, 'sidD418CapHP_F', None)
        gain = getattr(self.cfg, 'sidD418Gain', None)
        self._d418_r_ohm = float(roh) if roh is not None else 10000.0
        self._d418_c_lp_f = float(cap_lp) if cap_lp is not None else (10e-6 if self.model == '6581' else 470e-12)
        self._d418_c_hp_f = float(cap_hp) if cap_hp is not None else (10e-6 if self.model == '6581' else 1e-6)
        self._d418_gain = float(gain) if gain is not None else (0.35 if self.model == '6581' else 0.25)
        # Derived time constants
        self._d418_tau_lp = max(1e-9, self._d418_r_ohm * self._d418_c_lp_f)
        self._d418_tau_hp = max(1e-9, self._d418_r_ohm * self._d418_c_hp_f)
        self._d418_alpha_lp = 0.0
        self._d418_alpha_hp = 0.0
        self.set_sample_rate(self.sample_rate)

        # Per-voice smoothing for LUT combined waveforms (reduces zippering)
        self._wf_lp = [0.0, 0.0, 0.0]

        # Forensic SID cycle counter (phi2-aligned). This is incremented by update().
        # In a cycle-exact configuration, each update(1) corresponds to exactly one SID clock.
        self.sid_cycles_total: int = 0
        self._sid_cycle_now: int = 0

        SystemLogger.log('SID', f"SidChip init: clock_hz={self.clock_hz} seed={self._noise_seed:06X}", 'debug', category='boot')

    # ---------------------------------------------------------------------
    # Backwards-compatible telemetry accessors
    # ---------------------------------------------------------------------
    @property
    def env(self) -> List[int]:
        """Back-compat: older telemetry expects sid.env[v]."""
        return self.env_out

    @property
    def env_timer(self) -> List[int]:
        """Back-compat: older telemetry expects sid.env_timer[v].

        We expose the 15-bit rate counter, which is the primary timing source
        for envelope steps.
        """
        return self.rate_ctr

    def set_sample_rate(self, sample_rate: int) -> None:
        """Set audio sample rate and recompute analog-path coefficients."""
        sr = int(sample_rate)
        if sr <= 0:
            return
        self.sample_rate = sr
        dt = 1.0 / float(sr)
        # RC low-pass (volume DAC smoothing)
        self._d418_alpha_lp = dt / (self._d418_tau_lp + dt)
        # RC high-pass (AC coupling)
        self._d418_alpha_hp = self._d418_tau_hp / (self._d418_tau_hp + dt)

    def set_model(self, m: SidModel) -> None:
        self.model = m
        # Update model-dependent analog defaults unless overridden in cfg.
        cap_lp = getattr(self.cfg, 'sidD418CapLP_F', None)
        cap_hp = getattr(self.cfg, 'sidD418CapHP_F', None)
        gain = getattr(self.cfg, 'sidD418Gain', None)
        roh = getattr(self.cfg, 'sidD418Rohm', None)
        if roh is not None:
            self._d418_r_ohm = float(roh)
        if cap_lp is None:
            self._d418_c_lp_f = 10e-6 if m == '6581' else 470e-12
        else:
            self._d418_c_lp_f = float(cap_lp)
        if cap_hp is None:
            self._d418_c_hp_f = 10e-6 if m == '6581' else 1e-6
        else:
            self._d418_c_hp_f = float(cap_hp)
        if gain is None:
            self._d418_gain = 0.35 if m == '6581' else 0.25
        else:
            self._d418_gain = float(gain)
        self._d418_tau_lp = max(1e-9, self._d418_r_ohm * self._d418_c_lp_f)
        self._d418_tau_hp = max(1e-9, self._d418_r_ohm * self._d418_c_hp_f)
        self.set_sample_rate(self.sample_rate)
        SystemLogger.log('SID', f"Model set: {m} (D418 RC: R={self._d418_r_ohm}Ω LP_C={self._d418_c_lp_f}F HP_C={self._d418_c_hp_f}F)", 'debug', category='boot')

    def reset(self) -> None:
        SystemLogger.log('SID', 'Reset', 'debug', category='boot')
        for i in range(0x20):
            self.regs[i] = 0

        self.phase = [0, 0, 0]
        self.prev_phase = [0, 0, 0]

        # TEST-bit tracking (waveform reset behavior)
        self._test_last = [0, 0, 0]

        # OSC3/ENV3 read-behind latch (read returns previous-cycle values)
        self.osc3_latch = 0
        self.env3_latch = 0
        self._osc3_next = 0
        self._env3_next = 0

        self.env_ctr = [0, 0, 0]
        self.env_out = [0, 0, 0]
        self.env_state = ['R', 'R', 'R']
        self.hold_zero = [False, False, False]
        self.gate = [False, False, False]
        self._gate_pending = [None, None, None]
        self._gate_delay = [0, 0, 0]

        self.rate_ctr = [0, 0, 0]
        self.exp_ctr = [0, 0, 0]
        self.exp_period = [1, 1, 1]

        seed = int(getattr(self, '_noise_seed', 0x7FFFFF)) & 0x7FFFFF
        self.noise = [0x7FFFFF, 0x7FFFFF, seed if seed != 0 else 0x7FFFFF]

        self.filter_lp = 0.0
        self.filter_bp = 0.0
        self.filter_hp = 0.0

        self._ladder = [0.0, 0.0, 0.0, 0.0]
        self._ladder_last = 0.0

        self.osc3 = 0
        self.env3 = 0

        self._d418_lp = 0.0
        self._d418_hp = 0.0
        self._d418_lp_prev = 0.0
        self._wf_lp = [0.0, 0.0, 0.0]

        self.sid_cycles_total = 0

        SystemLogger.log('SID', f"SidChip reset: clock_hz={self.clock_hz} seed={seed:06X}", 'debug', category='boot')

    # ---------------------------------------------------------------------
    # Back-compat / telemetry-friendly properties
    # ---------------------------------------------------------------------
    @property
    def env(self) -> List[int]:
        """Backwards compatible envelope output array (3 voices)."""
        return self.env_out

    @property
    def env_timer(self) -> List[int]:
        """Backwards compatible timer/counter view (maps to rate counter)."""
        return self.rate_ctr

    def read(self, reg: int, bus_val: Optional[int] = None) -> int:
        r = reg & 0x1F
        if r in (0x19, 0x1A):
            return 0xFF
        if r == 0x1B:
            return int(self.osc3_latch) & 0xFF
        if r == 0x1C:
            return int(self.env3_latch) & 0xFF
        if bus_val is not None:
            return _u8(bus_val)
        return self.regs[r]

    def write(self, reg: int, val: int) -> None:
        r = reg & 0x1F
        v = _u8(val)

        # Gate handling on CTRL writes (1-cycle latch delay).
        if r in (0x04, 0x0B, 0x12):
            voice = 0 if r == 0x04 else (1 if r == 0x0B else 2)
            old = self.regs[r]
            old_gate = (old & 0x01) != 0
            new_gate = (v & 0x01) != 0
            if old_gate != new_gate:
                self._gate_pending[voice] = new_gate
                self._gate_delay[voice] = 1

        self.regs[r] = v

    # ---------------------------------------------------------------------
    # Timing
    # ---------------------------------------------------------------------
    def update(self, cpu_cycles: int) -> None:
        cc = max(0, int(cpu_cycles))
        if cc <= 0:
            return

        # Logic-analyzer-grade SID stepping requires per-cycle advancement so we can:
        #  - detect hard-sync wraps at the correct cycle
        #  - clock noise LFSR correctly (bit-19 rising edge behavior)
        #  - optionally emit per-cycle snapshots
        cycle_exact = bool(getattr(self.cfg, 'sidCycleExact', False))
        trace_every = int(getattr(self.cfg, 'sidTraceEveryCycles', 0) or 0)
        trace_cycle = SystemLogger.category_enabled('sidcycle')

        # Some SID features (hard sync / ring modulation / combined waveform phase relationships)
        # require per-phi2 stepping to be correct. Auto-enable the exact path when these are active.
        ctrl0 = self.regs[0x04] & 0xFF
        ctrl1 = self.regs[0x0B] & 0xFF
        ctrl2 = self.regs[0x12] & 0xFF
        sync_ring_active = ((ctrl0 | ctrl1 | ctrl2) & 0x06) != 0

        if cycle_exact or sync_ring_active or trace_every > 0 or trace_cycle:
            voices_set = set(int(v) for v in (getattr(self.cfg, 'sidTraceVoices', [0, 1, 2]) or [0, 1, 2]) if 0 <= int(v) <= 2)
            inc_wave = bool(getattr(self.cfg, 'sidTraceWave', True))
            inc_env = bool(getattr(self.cfg, 'sidTraceEnv', True))
            # If user turned on sidcycle category but didn't configure a rate, default to every cycle.
            if trace_cycle and trace_every <= 0:
                trace_every = 1

            for _ in range(cc):
                # Update OSC3/ENV3 read-behind latch (reads observe previous cycle)
                self.osc3_latch = int(self._osc3_next) & 0xFF
                self.env3_latch = int(self._env3_next) & 0xFF
                self.osc3 = int(self.osc3_latch) & 0xFF
                self.env3 = int(self.env3_latch) & 0xFF

                # Phase/noise per cycle
                prevs = [self.phase[0], self.phase[1], self.phase[2]]

                for i in range(3):
                    self.prev_phase[i] = prevs[i]
                    base_i = 0x00 if i == 0 else (0x07 if i == 1 else 0x0E)
                    ctrl_i = int(self.regs[base_i + 0x04]) & 0xFF
                    test_i = 1 if (ctrl_i & 0x08) else 0

                    # TEST-bit: on rising edge, reset phase accumulator and noise LFSR; while held, oscillator is stopped.
                    if test_i and not self._test_last[i]:
                        self.phase[i] = 0
                        self.prev_phase[i] = 0
                        self.noise[i] = 0x7FFFFF
                    self._test_last[i] = test_i
                    if test_i:
                        continue

                    freq = self._get_voice_freq(i)
                    ph_prev = prevs[i]
                    ph = _u24(ph_prev + freq)
                    self.phase[i] = ph

                    # Noise LFSR clocks on bit-19 rising edge of phase accumulator (reSID-style).
                    if (ph & 0x80000) and not (ph_prev & 0x80000):
                        self._clock_noise_once(i)
                        if SystemLogger.category_enabled('sidnoise'):
                            SystemLogger.log('SIDNOISE', f'v{i} lfsr_clock', 'debug', category='sidnoise', fields={
                                'sid_cycle': int(self.sid_cycles_total),
                                'voice': i,
                                'lfsr': int(self.noise[i]) & 0x7FFFFF,
                            })

                # Hard sync (reSID-style): sync resets happen on *bit-23 rising edge* of the source oscillator.
                rising = [((prevs[i] & 0x800000) == 0 and (self.phase[i] & 0x800000) != 0) for i in range(3)]
                for dest in range(3):
                    base_d = 0x00 if dest == 0 else (0x07 if dest == 1 else 0x0E)
                    ctrl_d = int(self.regs[base_d + 0x04]) & 0xFF
                    if not (ctrl_d & 0x02):
                        continue
                    src = _sid_chain_source(dest)
                    if rising[src]:
                        self.phase[dest] = 0

                # Envelopes per cycle
                for v in range(3):
                    self._clock_envelope(v, 1)

                self.sid_cycles_total += 1

                # Prepare next-cycle OSC3/ENV3 latch values.
                self._osc3_next = (int(self.phase[2]) >> 16) & 0xFF
                self._env3_next = int(self.env_out[2]) & 0xFF

                # Optional per-cycle snapshot (EXTREMELY noisy)
                if trace_every > 0 and (self.sid_cycles_total % trace_every == 0) and trace_cycle:
                    fields: Dict[str, Any] = {
                        'sid_cycle': int(self.sid_cycles_total),
                        'model': self.model,
                    }
                    for v in sorted(voices_set):
                        base = 0x00 if v == 0 else (0x07 if v == 1 else 0x0E)
                        ctrl = int(self.regs[base + 0x04]) & 0xFF
                        sub: Dict[str, Any] = {
                            'ctrl': ctrl,
                            'gate': bool(ctrl & 0x01),
                        }
                        if inc_wave:
                            sub.update({
                                'phase': int(self.phase[v]) & 0xFFFFFF,
                                'freq': int(self._get_voice_freq(v)) & 0xFFFF,
                                'pw': int(self._get_voice_pw(v)) & 0x0FFF,
                                'noise': int(self.noise[v]) & 0x7FFFFF,
                            })
                        if inc_env:
                            sub.update({
                                'env_state': self.env_state[v],
                                'env_ctr': int(self.env_ctr[v]) & 0xFF,
                                'env_out': int(self.env_out[v]) & 0xFF,
                                'rate_ctr': int(self.rate_ctr[v]) & 0x7FFF,
                                'exp_ctr': int(self.exp_ctr[v]) & 0xFFFF,
                                'exp_period': int(self.exp_period[v]) & 0xFFFF,
                            })
                        fields[f'v{v}'] = sub

                    SystemLogger.log('SIDCYCLE', 'sid_cycle', 'debug', category='sidcycle', fields=fields)

            # Commit latch-visible OSC3/ENV3 after the loop.
            self.osc3_latch = int(self._osc3_next) & 0xFF
            self.env3_latch = int(self._env3_next) & 0xFF
            self.osc3 = int(self.osc3_latch) & 0xFF
            self.env3 = int(self.env3_latch) & 0xFF
            return


        # Fast path: phase+noise in bulk; envelope stays cycle-granular via its own loop.
        for i in range(3):
            self.prev_phase[i] = self.phase[i]
            freq = self._get_voice_freq(i)
            self.phase[i] = _u24(self.phase[i] + (freq * cc))
            self._step_noise(i, freq, cc)

        # Hard sync wrap detection (coarse, but stable)
        for i in range(2):
            if self.phase[i] < self.prev_phase[i]:
                next_voice = i + 1
                next_base = 0x07 if next_voice == 1 else 0x0E
                next_ctrl = self.regs[next_base + 0x04]
                if next_ctrl & 0x02:
                    self.phase[next_voice] = 0

        for v in range(3):
            self._clock_envelope(v, cc)

        self.sid_cycles_total += cc
        # Update OSC3/ENV3 latch (no read-behind guarantee in fast path).
        self._osc3_next = (int(self.phase[2]) >> 16) & 0xFF
        self._env3_next = int(self.env_out[2]) & 0xFF
        self.osc3_latch = int(self._osc3_next) & 0xFF
        self.env3_latch = int(self._env3_next) & 0xFF
        self.osc3 = int(self.osc3_latch) & 0xFF
        self.env3 = int(self.env3_latch) & 0xFF

    # ---------------------------------------------------------------------
    # Audio
    # ---------------------------------------------------------------------
    def _dac12_to_float(self, v12: int, waveform_bits: int) -> float:
        """Convert a 12-bit DAC value to float [-1,1] with a small model-dependent nonlinearity."""
        x = (v12 & 0xFFF) / 4095.0
        if self.model == '6581':
            y = x ** 1.10
            out = (2.0 * y) - 1.0
            pop = bin(waveform_bits & 0x0F).count('1')
            if pop >= 2:
                out *= 0.85
            return out
        out = (2.0 * x) - 1.0
        return out * 0.95

    def render_sample(self) -> float:
        det = bool(getattr(self.cfg, 'deterministicDSP', False))
        if det:
            _Q = 16777216.0  # 2**24 fixed-point quantization
            def _q(x: float) -> float:
                return round(float(x) * _Q) / _Q
        else:
            def _q(x: float) -> float:
                return float(x)

        voice_out = [0.0, 0.0, 0.0]

        for v in range(3):
            base = 0x00 if v == 0 else (0x07 if v == 1 else 0x0E)
            ctrl = self.regs[base + 0x04]
            amp = (self.env_out[v] & 0xFF) / 255.0

            if ctrl & 0x08:  # TEST forces 0 on many chips
                continue

            ph = self.phase[v]
            waveform_bits = (ctrl >> 4) & 0x0F
            if waveform_bits == 0:
                continue

            # Ring modulation affects TRI component. Apply by inverting phase ramp if ring source MSB set.
            ring_mod = (ctrl & 0x04) != 0
            ring_source = _sid_chain_source(v) if ring_mod else 0
            ring_source_msb = ((self.phase[ring_source] >> 23) & 0x01) if ring_mod else 0
            # Build 12-bit waveform DAC value using reSID LUTs for combined waveforms.
            # Supports NOISE+other waves by AND-gating noise with the combined output (useful approximation).
            tri = bool(ctrl & 0x10)
            saw = bool(ctrl & 0x20)
            pul = bool(ctrl & 0x40)
            noi = bool(ctrl & 0x80)

            tri_saw_pul_mask = ctrl & 0x70
            pop_tsp = bin((tri_saw_pul_mask >> 4) & 0x07).count('1')

            v12 = 0
            wf = 0.0

            if pop_tsp >= 2 and tri_saw_pul_mask in (0x30, 0x50, 0x60, 0x70):
                lut = _ResIDWaveLUT.get(self.model, tri_saw_pul_mask)
                if lut is not None:
                    idx = (ph >> 12) & 0xFFF
                    if (tri_saw_pul_mask & 0x10) and ((((ph >> 23) & 0x01) ^ ring_source_msb) != 0):
                        idx ^= 0xFFF
                    v12 = int(lut[idx]) & 0xFFF
                    wf = self._dac12_to_float(v12, waveform_bits)

                    # light smoothing to reduce zippering (especially audible on 6581 combined)
                    lp = self._wf_lp[v]
                    a = 0.15 if self.model == '6581' else 0.10
                    lp = lp + a * (wf - lp)
                    self._wf_lp[v] = lp
                    wf = lp
                else:
                    wf = 0.0
            else:
                # Single-wave (or no LUT). Use simple generator(s).
                if tri and not (saw or pul):
                    idx = (ph >> 12) & 0xFFF
                    if (((ph >> 23) & 0x01) ^ ring_source_msb) != 0:
                        idx ^= 0xFFF
                    v12 = (idx << 1) & 0xFFF
                    wf = self._dac12_to_float(v12, waveform_bits)
                elif saw and not (tri or pul):
                    v12 = (ph >> 12) & 0xFFF
                    wf = self._dac12_to_float(v12, waveform_bits)
                elif pul and not (tri or saw):
                    pw = self._get_voice_pw(v)
                    v12 = 0xFFF if ((ph >> 12) & 0xFFF) < pw else 0x000
                    wf = self._dac12_to_float(v12, waveform_bits)
                else:
                    # Rare combos not in LUT set; fall back to average of active components.
                    acc = 0
                    n = 0
                    if tri:
                        idx = (ph >> 12) & 0xFFF
                        if (((ph >> 23) & 0x01) ^ ring_source_msb) != 0:
                            idx ^= 0xFFF
                        acc += (idx << 1) & 0xFFF
                        n += 1
                    if saw:
                        acc += (ph >> 12) & 0xFFF
                        n += 1
                    if pul:
                        pw = self._get_voice_pw(v)
                        acc += 0xFFF if ((ph >> 12) & 0xFFF) < pw else 0x000
                        n += 1
                    v12 = (acc // max(1, n)) & 0xFFF
                    wf = self._dac12_to_float(v12, waveform_bits)

            if noi:
                n = int(self.noise[v]) & 0x7FFFFF
                n12 = (n >> 11) & 0xFFF
                # If NOISE is combined with TRI+SAW+PULSE, many tunes rely on the chaotic mixed result.
                # We approximate this by XOR-mixing the LUT output with the noise DAC.
                if pop_tsp >= 3:
                    v12 = (v12 ^ n12) & 0xFFF
                elif pop_tsp > 0:
                    v12 = (v12 & n12) & 0xFFF
                else:
                    v12 = n12 & 0xFFF
                wf = self._dac12_to_float(v12, waveform_bits)

            wave = wf

            voice_out[v] = wave * amp

        # Voice 3 off bit (bit 7 of $D418)
        vol_control = self.regs[0x18]
        if vol_control & 0x80:
            voice_out[2] = 0.0

        filt_mode = self.regs[0x18]
        filt_route = self.regs[0x17]

        v1_filt = (filt_route & 0x01) != 0
        v2_filt = (filt_route & 0x02) != 0
        v3_filt = (filt_route & 0x04) != 0

        filt_in = 0.0
        direct_out = 0.0
        if v1_filt:
            filt_in += voice_out[0]
        else:
            direct_out += voice_out[0]
        if v2_filt:
            filt_in += voice_out[1]
        else:
            direct_out += voice_out[1]
        if v3_filt:
            filt_in += voice_out[2]
        else:
            direct_out += voice_out[2]

        # Filter (4-stage ladder-style; closer to SID sweeps than the toy SVF and can self-oscillate).
        filt_out = 0.0
        if (filt_in != 0.0) or ((filt_mode & 0x70) != 0):
            import math
            cutoff_lo = self.regs[0x15]
            cutoff_hi = self.regs[0x16]
            cutoff = ((cutoff_hi << 3) | (cutoff_lo & 0x07)) & 0x7FF  # 11-bit cutoff
            # Map cutoff register to a usable cutoff frequency. This is not a perfect measured curve,
            # but is model-shaped and stable, and supports resonance/self-oscillation.
            sr = float(self.sample_rate if self.sample_rate > 0 else 44100)
            x = cutoff / 2047.0
            if self.model == '6581':
                fc_hz = 30.0 + (x * x) * 8500.0
            else:
                fc_hz = 30.0 + (x ** 1.8) * 12500.0
            fc = max(1.0, min(fc_hz, 0.49 * sr)) / sr
            g = math.tan(math.pi * fc)
            scale = getattr(self.cfg, 'sidFilterCutoffScale', None)
            off_hz = getattr(self.cfg, 'sidFilterCutoffOffsetHz', None)
            if scale is not None:
                fc_hz *= float(scale)
            if off_hz is not None:
                fc_hz += float(off_hz)

            if SystemLogger.category_enabled('sidfilter'):
                _key = (int(cutoff), int((self.regs[0x17] >> 4) & 0x0F), int(filt_mode & 0x70))
                if _key != self._last_filter_regs:
                    self._last_filter_regs = _key
                    SystemLogger.log('SIDFILTER', 'filter_regs', 'debug', category='sidfilter', fields={
                        'cutoff': int(cutoff),
                        'res': int(_key[1]),
                        'mode': int(_key[2]),
                        'fc_hz': float(fc_hz),
                        'model': self.model,
                    })

            res = (self.regs[0x17] >> 4) & 0x0F
            k = 4.0 * (float(res) / 15.0)
            if self.model == '6581':
                k *= 1.05

            y1, y2, y3, y4 = self._ladder
            # Seed self-oscillation very lightly at extreme resonance (keeps silence stable otherwise).
            if filt_in == 0.0 and res >= 14:
                filt_in = ((self.noise[2] & 0x1) * 2.0 - 1.0) * 1e-6

            u = filt_in - k * y4

            def _sat(v: float) -> float:
                # Soft saturation (SID-ish nonlinearity proxy).
                return math.tanh(v)

            y1 = y1 + g * (_sat(u) - _sat(y1))
            y2 = y2 + g * (_sat(y1) - _sat(y2))
            y3 = y3 + g * (_sat(y2) - _sat(y3))
            y4 = y4 + g * (_sat(y3) - _sat(y4))
            y1=_q(y1); y2=_q(y2); y3=_q(y3); y4=_q(y4)
            self._ladder[0] = y1
            self._ladder[1] = y2
            self._ladder[2] = y3
            self._ladder[3] = y4

            lp = y4
            hp = u - y1
            bp = y1 - y4

            lp_en = (filt_mode & 0x10) != 0
            bp_en = (filt_mode & 0x20) != 0
            hp_en = (filt_mode & 0x40) != 0
            if lp_en:
                filt_out += lp
            if bp_en:
                filt_out += bp
            if hp_en:
                filt_out += hp

        mix = direct_out + filt_out

        # Master volume (lower 4 bits of $D418)
        vol = int(vol_control & 0x0F)
        vol_gain = vol / 15.0
        if SystemLogger.category_enabled('sidd418') and vol != self._last_vol:
            self._last_vol = vol
            SystemLogger.log('D418', 'volume_change', 'debug', category='sidd418', fields={
                'vol': int(vol),
                'vol_gain': float(vol_gain),
                'gain': float(self._d418_gain),
                'tau_lp': float(self._d418_tau_lp),
                'tau_hp': float(self._d418_tau_hp),
                'model': self.model,
            })


        # D418 digi path (volume DAC through board RC + AC coupling)
        gain = float(self._d418_gain)
        alpha_lp = float(self._d418_alpha_lp)
        alpha_hp = float(self._d418_alpha_hp)
        self._d418_lp += alpha_lp * (vol_gain - self._d418_lp)
        self._d418_hp = alpha_hp * (self._d418_hp + self._d418_lp - self._d418_lp_prev)
        self._d418_lp_prev = self._d418_lp
        if det:
            self._d418_lp = _q(self._d418_lp)
            self._d418_hp = _q(self._d418_hp)
            self._d418_lp_prev = _q(self._d418_lp_prev)
        digi_out = self._d418_hp * gain

        out = (mix * vol_gain) / 3.0
        out = out + digi_out
        if det:
            out = _q(out)
        return _clamp(out, -1.0, 1.0)

    # ---------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------
    def _get_voice_freq(self, v: int) -> int:
        base = 0x00 if v == 0 else (0x07 if v == 1 else 0x0E)
        lo = self.regs[base + 0x00]
        hi = self.regs[base + 0x01]
        return (hi << 8) | lo

    def _get_voice_pw(self, v: int) -> int:
        base = 0x00 if v == 0 else (0x07 if v == 1 else 0x0E)
        lo = self.regs[base + 0x02]
        hi = self.regs[base + 0x03] & 0x0F
        return (hi << 8) | lo

    def _step_noise(self, v: int, freq: int, cycles: int) -> None:
        if freq == 0:
            return
        total_phase_inc = freq * cycles
        shifts = total_phase_inc >> 19
        if shifts <= 0:
            return
        x = self.noise[v] & 0x7FFFFF
        for _ in range(min(int(shifts), 128)):
            b = ((x >> 22) ^ (x >> 17)) & 1
            x = ((x << 1) & 0x7FFFFF) | b
        self.noise[v] = x

    def _clock_noise_once(self, v: int) -> None:
        """Clock the 23-bit noise LFSR one step (reSID polynomial)."""
        x = self.noise[v] & 0x7FFFFF
        b = ((x >> 22) ^ (x >> 17)) & 1
        self.noise[v] = ((x << 1) & 0x7FFFFF) | b

    def _exp_period_for_level(self, level: int) -> int:
        # Map envelope level to exponential divider period.
        # Ranges are inclusive of upper bound.
        if level >= 0x5D:
            return 1
        if level >= 0x36:
            return 2
        if level >= 0x1A:
            return 4
        if level >= 0x0E:
            return 8
        if level >= 0x06:
            return 16
        return 30

    def _apply_gate_latch(self, v: int) -> None:
        if self._gate_delay[v] > 0:
            self._gate_delay[v] -= 1
            if self._gate_delay[v] == 0:
                pending = self._gate_pending[v]
                self._gate_pending[v] = None
                if pending is not None:
                    # Gate ON: enter ATTACK; Gate OFF: enter RELEASE
                    if pending:
                        self.gate[v] = True
                        self.env_state[v] = 'A'
                        self.hold_zero[v] = False
                        # Exponential counter resets on state change (rate counter does NOT reset; delay bug preserved).
                        self.exp_ctr[v] = 0
                        self.exp_period[v] = 1
                    else:
                        self.gate[v] = False
                        self.env_state[v] = 'R'
                        self.exp_ctr[v] = 0
                        self.exp_period[v] = self._exp_period_for_level(self.env_ctr[v])

    def _clock_envelope(self, v: int, cycles: int) -> None:
        """Advance envelope by SID clocks with reSID-style quirks.

        This is intentionally cycle-granular because the nastier edge-cases
        (rate-counter wrap-delay, gate latch timing, and exponential divider)
        depend on exact counter progression.
        """
        if cycles <= 0:
            return

        base = 0x00 if v == 0 else (0x07 if v == 1 else 0x0E)
        pipeline = bool(getattr(self.cfg, 'enableAdsrPipeline', True))

        # Enable verbose envelope tracing only when explicitly requested.
        # This can still be extremely noisy if you render for seconds.
        env_trace = SystemLogger.category_enabled('sidenv') and bool(getattr(self.cfg, 'sidCycleExact', False) or int(getattr(self.cfg, 'sidTraceEveryCycles', 0) or 0) > 0)

        for _ in range(int(cycles)):
            # Gate takes effect with a 1-cycle latch delay.
            self._apply_gate_latch(v)

            # Envelope output pipeline: output is the *previous* internal value for this cycle.
            if pipeline:
                self.env_out[v] = self.env_ctr[v] & 0xFF

            ad = int(self.regs[base + 0x05]) & 0xFF
            sr = int(self.regs[base + 0x06]) & 0xFF
            atk = (ad >> 4) & 0x0F
            dec = ad & 0x0F
            sus = (sr >> 4) & 0x0F
            rel = sr & 0x0F
            sus_level = sus * 17

            st = self.env_state[v]
            if bool(getattr(self, '_ad_glitch', [0,0,0])[v]):
                # Apply the deferred exp_period reset after the 1-cycle A->D glitch.
                self.exp_period[v] = 1
                self._ad_glitch[v] = 0
            if st == 'A':
                rate_period = self._RATE_PERIODS[atk]
            elif st == 'R':
                rate_period = self._RATE_PERIODS[rel]
            else:
                rate_period = self._RATE_PERIODS[dec]

            # Rate counter increments modulo 32768 and triggers only on equality.
            rc = (self.rate_ctr[v] + 1) & 0x7FFF
            self.rate_ctr[v] = rc

            if rc != (rate_period & 0x7FFF):
                continue

            # On match, SID resets the rate counter to 0 and performs a step.
            self.rate_ctr[v] = 0

            e_before = self.env_ctr[v] & 0xFF
            st_before = st
            e = e_before

            # Hold-zero latch: in real 6581/8580 the envelope can "stick" at 0 in release
            # until a new gate-on clears the latch.
            if st == 'R' and self.hold_zero[v]:
                # Still latch output through pipeline.
                if not pipeline:
                    self.env_out[v] = self.env_ctr[v] & 0xFF
                if env_trace:
                    SystemLogger.log('SIDENV', 'hold_zero', 'debug', category='sidenv', fields={
                        'sid_cycle': int(self.sid_cycles_total) + 1,
                        'voice': v,
                        'state': 'R',
                        'env': int(e),
                        'rate_period': int(rate_period),
                        'rate_ctr': int(self.rate_ctr[v]) & 0x7FFF,
                    })
                continue

            if st == 'A':
                if e < 0xFF:
                    e += 1
                if e >= 0xFF:
                    e = 0xFF
                    # Attack->Decay transition resets exponential divider.
                    self.env_state[v] = 'D'
                    self.exp_ctr[v] = 0
                    if bool(getattr(self.cfg, 'enableAdsrADGlitch', True)):
                        # Subtle SID quirk: exp_period can be stale for 1 cycle after A->D.
                        # We emulate this by deferring the exp_period reset until the next SID clock.
                        self._ad_glitch[v] = 1
                    else:
                        self.exp_period[v] = 1

            elif st == 'D' or st == 'S':
                # Decay/sustain: divider slows the decrement in the tail.
                if e > sus_level:
                    self.exp_ctr[v] += 1
                    if self.exp_ctr[v] >= self.exp_period[v]:
                        self.exp_ctr[v] = 0
                        e = max(sus_level, e - 1)
                        new_p = self._exp_period_for_level(e)
                        if new_p != self.exp_period[v]:
                            # reSID-style: reset exp counter on period change.
                            self.exp_ctr[v] = 0
                        self.exp_period[v] = new_p
                else:
                    # Sustain reached
                    self.env_state[v] = 'S'

            else:  # Release
                if e > 0:
                    self.exp_ctr[v] += 1
                    if self.exp_ctr[v] >= self.exp_period[v]:
                        self.exp_ctr[v] = 0
                        e = max(0, e - 1)
                        new_p = self._exp_period_for_level(e)
                        if new_p != self.exp_period[v]:
                            self.exp_ctr[v] = 0
                        self.exp_period[v] = new_p
                        if e == 0:
                            # Latch hold-zero once we hit zero in release.
                            self.hold_zero[v] = True

            self.env_ctr[v] = e & 0xFF

            # If pipeline is disabled, output tracks internal immediately.
            if not pipeline:
                self.env_out[v] = self.env_ctr[v] & 0xFF

            if env_trace and (e != e_before or self.env_state[v] != st_before):
                SystemLogger.log('SIDENV', 'step', 'debug', category='sidenv', fields={
                    'sid_cycle': int(self.sid_cycles_total) + 1,
                    'voice': v,
                    'state': self.env_state[v],
                    'state_prev': st_before,
                    'env_prev': int(e_before),
                    'env': int(self.env_ctr[v]) & 0xFF,
                    'env_out': int(self.env_out[v]) & 0xFF,
                    'atk': int(atk),
                    'dec': int(dec),
                    'sus_level': int(sus_level),
                    'rel': int(rel),
                    'rate_period': int(rate_period),
                    'rate_ctr': int(self.rate_ctr[v]) & 0x7FFF,
                    'exp_ctr': int(self.exp_ctr[v]) & 0xFFFF,
                    'exp_period': int(self.exp_period[v]) & 0xFFFF,
                    'hold_zero': bool(self.hold_zero[v]),
                })

        # Keep output synced if pipeline is disabled.
        if not pipeline:
            self.env_out[v] = self.env_ctr[v] & 0xFF
