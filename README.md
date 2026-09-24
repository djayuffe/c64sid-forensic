# C64SID Forensic

`c64sid-forensic` is a dependency-free Python core for loading PSID/RSID files,
running their 6502 init/play routines in a C64 memory map, modelling one to
three SID chips, and exporting deterministic playback evidence.

It is designed for analysis and tooling rather than claiming analog-perfect
emulation. The core deliberately exposes timing, register, bus, and telemetry
data so a caller can inspect how a tune behaves.

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

The installed import package is `sid`:

```python
from sid.playback import PlaybackCoordinator

with open("music.sid", "rb") as source:
    player = PlaybackCoordinator()
    player.load_sid_bytes(source.read())
    result = player.render_wav("music.wav", seconds=30)
print(result)
```

## Development checks

```bash
python3 -m compileall -q .
PYTHONPATH=.. python3 -m unittest discover -s tests -v
python3 -m pip install .
```

## Limits

This is not a complete C64 or analog SID replacement. Cartridge modes, full
VIC fetch/collision behaviour, and all analog component variations are outside
the core’s scope. Treat rendered audio and traces as reproducible analytical
results, then validate hardware-critical conclusions against VICE and real
hardware.

## License

MIT. See [LICENSE](LICENSE).
