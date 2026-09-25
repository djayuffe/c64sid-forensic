# C64SID Forensic

`c64sid-forensic` is a dependency-free Python 3.10+ core for loading PSID/RSID
files, running their 6502 init/play routines in a C64 memory map, modelling one
to three SID chips, rendering WAV audio, and exporting deterministic playback
evidence.

It is designed for analysis and tooling rather than claiming analog-perfect
emulation. The core deliberately exposes timing, register, bus, and telemetry
data so a caller can inspect how a tune behaves.

> Status: **v1.1.1** is a tested analysis core. It is suitable for metadata,
> deterministic playback experiments, WAV rendering, and SID-PRO evidence—not
> as a substitute for VICE or real hardware validation.

## Features

- PSID v1–v4 and RSID header parsing, including second/third SID metadata.
- C64 RAM/ROM banking, 6510 CPU port handling, CIA timers, VIC-II raster IRQs,
  and VIC badline/sprite-pointer CPU bus steals.
- 6581/8580-oriented SID register, oscillator, envelope, filter, combined-wave
  LUT, and `$D418` digi-path modelling.
- VBI-style playback coordinator that renders 16-bit PCM WAV.
- SID-PRO V6 JSON export with compressed snapshots, event stream, checksums,
  and telemetry.
- Explicit HLE/ROM policy: RSID requires real ROMs unless HLE is deliberately
  enabled with `allowHleForRsid=True`.

## Layout

| Path | Purpose |
| --- | --- |
| `sid_parser.py` | PSID/RSID header validation and tune payload extraction |
| `c64_system.py` | CPU, memory, VIC, CIA, SID scheduling and interrupts |
| `sid_chip.py` | SID voice, envelope, filter, and audio model |
| `memory_bank.py` | C64 address map, ROM banking, I/O, and SID routing |
| `playback/` | High-level SID loader, renderer, and forensic exporter |
| `sid_exporter.py` | Streamed SID-PRO V6 JSON writer |

## Install

```bash
python3 -m pip install .
```

The installed import package is `sid`. The project has no runtime dependencies.

## Quick start

```bash
# Read metadata only; tune code is not executed.
python3 -m sid music.sid --info

# Render 30 seconds to a standard mono, 16-bit PCM WAV file.
python3 -m sid music.sid --wav music.wav --seconds 30
```

The command exits with a useful parser error for an invalid SID header. Preserve
the original SID alongside exported WAV/SID-PRO files when comparing results.

## Command-line use

Inspect a tune without executing its code:

```bash
python3 -m sid music.sid --info
```

Render a PSID tune and produce a forensic SID-PRO V6 JSON capture:

```bash
python3 -m sid music.sid --wav music.wav --seconds 60 --sidpro music.sidpro.json
```

RSID files require real C64 ROMs for faithful execution. `--allow-hle-rsid`
exists for exploratory use only; its generated ROM stubs are intentionally not
presented as cycle- or hardware-faithful.

## Python examples

```python
from sid.playback import PlaybackCoordinator

with open("music.sid", "rb") as source:
    player = PlaybackCoordinator()
    player.load_sid_bytes(source.read())
    result = player.render_to_wav("music.wav", seconds=30)
print(result)
```

For metadata only, no emulation is necessary:

```python
from sid import parse_sid_header

header, program = parse_sid_header(open("music.sid", "rb").read())
print(header.title, header.sidAddresses, len(program))
```

Runnable versions live in [`examples/`](examples/).

## Correctness model

The parser rejects malformed magic/version, impossible song selection, and
truncated/invalid header offsets. SID chip addresses are validated against the
PSID address map, including its reserved range. The emulator progresses in
PHI2 cycles: CPU bus operations, VIC DMA stalls, CIA/VIC ticking, and SID
advancement share a clock. The system API is deliberately explicit:

- `C64System.step(cycles)` advances a bounded number of global PHI2 cycles.
- `C64System.call(address, a, x, y)` invokes a 6502 subroutine and returns on
  the outer `RTS`, with a safety bound.
- `PlaybackCoordinator.enable_forensic_dump(path)` must be called before
  `load_sid_bytes` so init and playback events are captured.

See [`docs/architecture.md`](docs/architecture.md) for the component map and
known fidelity boundaries.

## Output formats

| Output | When to use it | What it contains |
| --- | --- | --- |
| `.wav` | Listening, waveform analysis, or regression comparison | Mono 16-bit PCM at the requested sample rate |
| `.sidpro.json` | Deterministic inspection or diffing | Compressed RAM snapshots, SID writes, checksums, and frame telemetry |

SID-PRO capture is opt-in with `--sidpro path.sidpro.json` or
`enable_forensic_dump()`. It must be enabled before loading the tune to include
the init routine.

## Development checks

```bash
/opt/local/bin/python3.10 -m compileall -q .
PYTHONPATH=.. /opt/local/bin/python3.10 -m unittest discover -s tests -v
/opt/local/bin/python3.10 -m pip install .
```

## Limits

This is not a complete C64 or analog SID replacement. Cartridge modes, full
VIC fetch/collision behaviour, exact sprite DMA memory addressing, IEC/tape,
and all analog component variations are outside the core’s scope. Treat
rendered audio and traces as reproducible analytical results, then validate
hardware-critical conclusions against VICE and real hardware.

## License

MIT. See [LICENSE](LICENSE).
