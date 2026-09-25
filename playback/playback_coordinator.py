from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..logger import SystemLogger
from ..c64_system import C64System
from ..hle import HLE
from ..sid_chip import SidChip
from ..sid_exporter import SidExporter
from ..sid_parser import parse_sid_header
from ..sid_types import C64Config, SidHeader
from ..sidpro_recorder import CycleCounterTrace, SidProEventRecorder


def _i16(x: float) -> int:
    if x < -1.0: x = -1.0
    if x > 1.0: x = 1.0
    return int(round(x * 32767.0))


@dataclass
class PlaybackResult:
    wav_path: str
    frames_rendered: int
    samples: int


class PlaybackCoordinator:
    """Standalone coordinator that loads a SID file and renders PCM audio.

    - Runs the C64 core for init + play calls (VBI style)
    - Generates WAV at target sample_rate

    Notes:
    - For RSID tunes requiring ROMs, enableHle=True provides minimal stubs.
    """

    def __init__(self, cfg: Optional[C64Config] = None):
        self.cfg = cfg or C64Config()
        self.c64 = C64System(self.cfg)
        self.header: Optional[SidHeader] = None
        self.sid_data: Optional[bytes] = None
        self.selected_song: Optional[int] = None

        # Forensic dump / SID-PRO V6
        self._dump_json_path: Optional[str] = None
        self._dump_compression: str = 'zlib'
        self._cycle_trace: Optional[CycleCounterTrace] = None
        self._sidpro: Optional[SidProEventRecorder] = None
        self._ram_initial: bytes = b""
        self._telemetry_frames: list[dict] = []
        self._trace_start_cycles: int = 0
        self._telemetry_frame_index: int = 0

    def enable_forensic_dump(self, json_path: str, compression: str = 'zlib') -> None:
        """Enable bus tracing + forensic JSON export.

        Call this BEFORE load_sid_bytes() so we can capture init/play.
        """
        self._dump_json_path = str(json_path)
        self._dump_compression = str(compression)
        SystemLogger.log('Playback', f"Forensic dump armed: {self._dump_json_path} | compression={self._dump_compression}", 'debug', category='boot')

    def load_sid_bytes(self, raw: bytes, song: Optional[int] = None) -> None:
        """Load a SID file and select a one-based subsong for initialization."""
        hdr, mem = parse_sid_header(raw)
        selected_song = hdr.startSong if song is None else int(song)
        if not 1 <= selected_song <= hdr.songs:
            raise ValueError(f"Song {selected_song} is outside 1..{hdr.songs}")
        self.header = hdr
        self.sid_data = mem
        self.selected_song = selected_song

        SystemLogger.log('Playback', f"Loaded '{hdr.title}' by {hdr.author} ({hdr.released})", 'info')
        SystemLogger.log('Playback', f"System: {'NTSC' if hdr.isNtsc else 'PAL'} | SIDs: {hdr.sidCount} | BASIC: {hdr.c64BasicFlag}", 'info')

        # Setup machine
        self.c64.set_model(hdr.isNtsc)
        SystemLogger.log('Playback', f"Model set: {'NTSC' if hdr.isNtsc else 'PAL'} | cycles/frame={self.c64.model.cyclesPerFrame}", 'debug', category='boot')

        # Install ROMs
        #
        # IMPORTANT (for correctness): RSID expects real ROM behavior. Running
        # RSID with missing ROMs (or with HLE stubs) is *not* logic-analyzer-faithful
        # and can break compatibility. We therefore:
        #   - Prefer external ROMs if present
        #   - Otherwise: only allow HLE for RSID if explicitly enabled
        is_rsid = (hdr.magic == 'RSID')

        have_ext = bool(self.cfg.roms.kernal) or bool(self.cfg.roms.basic) or bool(self.cfg.roms.chargen)

        if have_ext:
            self.c64.memory.install_roms(self.cfg.roms.basic, self.cfg.roms.kernal, self.cfg.roms.chargen)
            SystemLogger.log('Boot', f"Installed external ROMs: BASIC={'yes' if self.cfg.roms.basic else 'no'} KERNAL={'yes' if self.cfg.roms.kernal else 'no'} CHARGEN={'yes' if self.cfg.roms.chargen else 'no'}", 'debug', category='boot')
        else:
            if is_rsid and not (self.cfg.enableHle and getattr(self.cfg, 'allowHleForRsid', False)):
                raise RuntimeError(
                    "RSID tune requires real C64 ROMs (KERNAL + CHARGEN, BASIC usually). "
                    "Provide ROM images via C64Config.roms or run with --enable-hle --allow-hle-rsid to use stub ROMs. "
                    "(Stub/HLE mode is not cycle-faithful and has limited compatibility.)"
                )

            if self.cfg.enableHle:
                self.c64.memory.install_roms(HLE.generate_basic(), HLE.generate_kernal(), None)
                SystemLogger.log('Boot', 'Installed HLE BASIC+KERNAL ROM stubs (CHARGEN=None)', 'warn' if is_rsid else 'debug', category='boot')
                if is_rsid:
                    SystemLogger.log('Boot', 'WARNING: RSID running in HLE stub ROM mode; compatibility and side-effects are limited.', 'warn', category='boot')
            else:
                self.c64.memory.install_roms(None, None, None)
                SystemLogger.log('Boot', 'No ROMs installed (RAM-only).', 'warn', category='boot')

        # Attach SIDs
        sids = []
        for i in range(hdr.sidCount):
            sid = SidChip(hdr.clockFreq, self.cfg)
            sid.set_model(hdr.sidModels[i])
            sids.append(sid)
        self.c64.attach_sids(sids, hdr.sidAddresses)
        SystemLogger.log('Boot', f"Attached {len(sids)} SID chip(s): bases={[hex(x) for x in hdr.sidAddresses]} models={hdr.sidModels}", 'debug', category='boot')

        # Load tune data
        self.c64.load_program(hdr.loadAddress, mem)
        SystemLogger.log('Boot', f"Loaded tune data: {len(mem)} bytes @ ${hdr.loadAddress:04X}", 'debug', category='boot')

        # Reset system (ROMs already present)
        # ------------------------------------------------------------------
        # Deterministic bus-cycle indexing
        #
        # If verbose memory/SID logging or forensic dump is enabled, we must
        # have a cycle counter trace installed *before* the reset vector fetch.
        # Otherwise the first observable bus operations will show cycle=-1 and
        # later start at cycle=0, which breaks diffing and reference traces.
        #
        # Fix: install a lightweight CycleCounterTrace here and force cycle=0.
        # ------------------------------------------------------------------
        need_cycle_trace = bool(self._dump_json_path) or (
            SystemLogger.category_enabled('memrw')
            or SystemLogger.category_enabled('memread')
            or SystemLogger.category_enabled('memwrite')
            or SystemLogger.category_enabled('memio')
            or SystemLogger.category_enabled('sidregs')
            or SystemLogger.category_enabled('sidvol')
            or SystemLogger.category_enabled('sidfilter')
            or SystemLogger.category_enabled('digi')
        )

        if need_cycle_trace and self._cycle_trace is None:
            self._cycle_trace = CycleCounterTrace()
            try:
                self._cycle_trace.set_cycle(0)
            except Exception:
                pass
            self.c64.cpu.set_trace(self._cycle_trace)
            SystemLogger.log('Trace', 'Installed CycleCounterTrace (pre-reset) for deterministic cycle indices', 'debug', category='boot')

        self.c64.cpu.reset()
        SystemLogger.log('Boot', f"CPU reset complete: PC=${self.c64.cpu.pc:04X} SP=${self.c64.cpu.sp:02X} A=${self.c64.cpu.a:02X} X=${self.c64.cpu.x:02X} Y=${self.c64.cpu.y:02X}", 'debug', category='boot')

        # If forensic dump enabled, attach SID write capture and snapshot RAM.
        if self._dump_json_path:
            # Observe CPU writes and record only SID register writes (SID-PRO bus_events)
            self._sidpro = SidProEventRecorder(hdr.sidAddresses)
            self.c64.cpu.set_write_observer(self._sidpro.observe_write)
            SystemLogger.log('Forensic', 'CPU write observer attached for SID writes (SID-PRO bus_events)', 'debug', category='boot')
            # Capture initial RAM snapshot (post-reset, pre-init)
            self._ram_initial = bytes(self.c64.memory.ram)
            self._telemetry_frames = []
            self._trace_start_cycles = int(self.c64.stats.cpuCycles)
            self._telemetry_frame_index = 0
        # Conventional SID init input: zero-based subsong number in A.
        self.c64.cpu.a = (selected_song - 1) & 0xFF

        # Call init
        if hdr.initAddress != 0:
            SystemLogger.log('Playback', f"Calling init at ${hdr.initAddress:04X} (song {selected_song})", 'info')
            self.c64.call(hdr.initAddress, a=(selected_song - 1) & 0xFF, x=0, y=0)
        else:
            SystemLogger.log('Playback', 'No init address; skipping init', 'warn')

    def render_to_wav(self, out_path: str, seconds: float = 10.0, sample_rate: int = 44100) -> PlaybackResult:
        if self.header is None or self.sid_data is None:
            raise RuntimeError('No SID loaded')

        if not 8000 <= sample_rate <= 96000:
            raise ValueError(f"Sample rate {sample_rate} out of range [8000-96000]")

        hdr = self.header
        frames = int(max(0.0, float(seconds)) * sample_rate)

        # Inform SID core about output sample rate (used by D418 RC/digi path and any sample-domain effects).
        if getattr(self.c64, 'sids', None):
            for sid in self.c64.sids:
                try:
                    sid.set_sample_rate(sample_rate)
                except Exception:
                    pass

        # Determine play cadence
        # Check if CIA timer based (speed bit pattern) or VBI
        use_cia_timing = False
        cia_frequency = 60.0  # Default to 60Hz if CIA-based

        if hdr.playAddress != 0:
            # PSID speed bit N applies only to subsong N+1. The 32-bit field
            # cannot describe subsongs beyond the first 32.
            song_index = (self.selected_song or hdr.startSong) - 1
            if 0 <= song_index < 32 and (hdr.speed & (1 << song_index)):
                use_cia_timing = True
                # Estimate frequency from timer (rough approximation)
                cia_frequency = 50.0 if not hdr.isNtsc else 60.0

        cycles_per_frame = self.c64.model.cyclesPerFrame

        if use_cia_timing:
            # CIA-based: call play routine at CIA frequency
            cycles_per_play = int(hdr.clockFreq / cia_frequency)
        else:
            # VBI-based: call play routine once per frame
            cycles_per_play = cycles_per_frame

        cycles_per_sample = hdr.clockFreq / float(sample_rate)

        # Simple fractional accumulator for CPU cycles per output sample
        cpu_acc = 0.0

        outp = Path(out_path)
        outp.parent.mkdir(parents=True, exist_ok=True)

        with wave.open(str(outp), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)

            play_cycle_acc = 0
            frames_rendered = 0

            # Telemetry capture at VIC frame rate (50/60Hz), relative to trace start
            next_frame_cycle = 0

            for n in range(frames):
                # advance emulation to match audio time
                cpu_acc += cycles_per_sample
                cyc = int(cpu_acc)
                if cyc > 0:
                    cpu_acc -= cyc
                    self.c64.step(cyc)
                    play_cycle_acc += cyc

                if self._dump_json_path and self._cycle_trace is not None:
                    # Capture telemetry at VIC frame rate (50/60 Hz)
                    rel_cycles = int(self.c64.stats.cpuCycles) - int(self._trace_start_cycles)
                    while rel_cycles >= next_frame_cycle:
                        self._telemetry_frames.append(self._make_frame(hdr.clockFreq))
                        next_frame_cycle += int(self.c64.model.cyclesPerFrame)
                        rel_cycles = int(self.c64.stats.cpuCycles) - int(self._trace_start_cycles)

                # Play call when we cross the play boundary
                if hdr.playAddress != 0 and play_cycle_acc >= cycles_per_play:
                    # Can cross multiple play calls if sample rate low vs cpu; loop
                    while play_cycle_acc >= cycles_per_play:
                        play_cycle_acc -= cycles_per_play
                        self.c64.call(hdr.playAddress, a=0, x=0, y=0)
                        frames_rendered += 1

                # Render audio sample - mix all SIDs
                s = 0.0
                if self.c64.sids:
                    for sid in self.c64.sids:
                        s += sid.render_sample()
                    s /= len(self.c64.sids)  # Average multiple SIDs

                wf.writeframesraw(_i16(s).to_bytes(2, byteorder='little', signed=True))

                # Write SID-PRO V6 forensic JSON if requested
        if self._dump_json_path and self._sidpro is not None:
            bus_cycles_f64le, bus_events_u8, _count = self._sidpro.snapshot()

            # Map SID models to spec: 0=6581, 1=8580
            sid_models = [0 if m == '6581' else 1 for m in hdr.sidModels]
            clock_type = 1 if hdr.isNtsc else 0
            frame_rate = 60.0 if hdr.isNtsc else 50.0

            with open(self._dump_json_path, 'w', encoding='utf-8') as fp:
                SidExporter.write_sidpro_json(
                    fp,
                    clock_type=clock_type,
                    clock_hz=int(hdr.clockFreq),
                    frame_rate=float(frame_rate),
                    sid_models=sid_models,
                    sid_addresses=list(hdr.sidAddresses),
                    bus_cycles_f64le=bus_cycles_f64le,
                    bus_events_u8=bus_events_u8,
                    ram_initial=self._ram_initial,
                    ram_final=bytes(self.c64.memory.ram),
                    telemetry_frames=self._telemetry_frames,
                    generator=SidExporter.DEFAULT_GENERATOR,
                    compression=self._dump_compression,
                    analysis={},
                )
            SystemLogger.log('Playback', f'Wrote SID-PRO forensic dump: {self._dump_json_path}', 'info')

            # Detach trace/observer to avoid accidental growth on reuse
            self.c64.cpu.set_write_observer(None)
            self.c64.cpu.set_trace(None)

        return PlaybackResult(str(outp), frames_rendered, frames)

    def _make_frame(self, clock_hz: int) -> dict:
        """Build one SID-PRO telemetry frame (silicon state snapshot)."""
        chips: list[dict] = []
        for sid in self.c64.sids:
            voices: list[dict] = []
            for v in range(3):
                base = 0x00 if v == 0 else (0x07 if v == 1 else 0x0E)
                ctrl = int(sid.regs[base + 0x04]) & 0xFF
                ad = int(sid.regs[base + 0x05]) & 0xFF
                sr = int(sid.regs[base + 0x06]) & 0xFF

                st_map = {'A': 0, 'D': 1, 'S': 2, 'R': 3}
                env_state = int(st_map.get(sid.env_state[v], 3))

                # Rate index depends on state
                if env_state == 0:      # ATK
                    rate = (ad >> 4) & 0x0F
                elif env_state == 1:    # DEC
                    rate = ad & 0x0F
                elif env_state == 3:    # REL
                    rate = sr & 0x0F
                else:                   # SUS
                    rate = 0

                voices.append({
                    "osc": {
                        "acc": int(sid.phase[v]) & 0xFFFFFF,
                        "lfsr": int(sid.noise[v]) & 0x7FFFFF,
                    },
                    "env": {
                        "out": int(sid.env[v]) & 0xFF,
                        "state": env_state,
                        "counter": int(sid.env_timer[v]) if hasattr(sid, 'env_timer') else 0,
                        "rate": int(rate),
                    },
                    "regs": {
                        "freq": int(sid._get_voice_freq(v)) & 0xFFFF,
                        "pw": int(sid._get_voice_pw(v)) & 0x0FFF,
                        "ctrl": int(ctrl),
                        "ad": int(ad),
                        "sr": int(sr),
                    },
                    "derived": {
                        # wave is stored as the 4-bit waveform selector (bitmask, 0..15)
                        "wave": int((ctrl >> 4) & 0x0F),
                        "test": bool(ctrl & 0x08),
                        "ring": bool(ctrl & 0x04),
                        "sync": bool(ctrl & 0x02),
                        "gate": bool(ctrl & 0x01),
                    }
                })

            # Filter state (11-bit cutoff, 4-bit resonance)
            cutoff_lo = int(sid.regs[0x15]) & 0xFF
            cutoff_hi = int(sid.regs[0x16]) & 0xFF
            cutoff = ((cutoff_hi << 3) | (cutoff_lo & 0x07)) & 0x7FF
            resonance = (int(sid.regs[0x17]) >> 4) & 0x0F
            voice_mask = int(sid.regs[0x17]) & 0x07
            mode = int(sid.regs[0x18]) & 0xFF

            # 8580 integrator states (quantized fixed-point for determinism)
            if getattr(sid, 'model', 'UNKNOWN') == '8580':
                hp_int = int(round(float(getattr(sid, 'filter_hp', 0.0)) * (1 << 24)))
                bp_int = int(round(float(getattr(sid, 'filter_bp', 0.0)) * (1 << 24)))
            else:
                hp_int = 0
                bp_int = 0

            chips.append({
                "voices": voices,
                "filter": {
                    "cutoff": int(cutoff),
                    "resonance": int(resonance),
                    "mode": int(mode),
                    "voice_mask": int(voice_mask),
                    "hp_int": int(hp_int),
                    "bp_int": int(bp_int),
                }
            })

        cyc_rel = int(self.c64.stats.cpuCycles) - int(self._trace_start_cycles)
        frame = {
            "frame": int(self._telemetry_frame_index),
            "cycle": int(cyc_rel),
            "chips": chips,
        }
        self._telemetry_frame_index += 1
        return frame
