# Getting started

## Requirements

- Python 3.10 or newer.
- No runtime Python dependencies.
- A legal `.sid` file. C64 ROM images are required for faithful RSID execution.

Install from a checkout:

```bash
python3 -m pip install .
```

## Inspect before executing

Use metadata inspection first when cataloguing untrusted or unknown tunes. It
parses the SID header but does not execute 6502 code.

```bash
python3 -m sid tune.sid --info
```

The output includes title, author, load/init/play addresses, timing model, and
mapped SID chip addresses.

## Render a PSID file

```bash
python3 -m sid tune.sid --wav tune.wav --seconds 60 --sample-rate 44100
```

The output is a mono 16-bit PCM WAV. The renderer uses the SID header’s PAL or
NTSC clock and calls the tune’s play routine at its declared cadence. Select a
one-based subsong explicitly when needed:

```bash
python3 -m sid tune.sid --song 2 --wav song-2.wav
```

## Capture forensic evidence

```bash
python3 -m sid tune.sid --wav tune.wav --sidpro tune.sidpro.json
```

Enable the dump before `load_sid_bytes()` in Python code. That ordering captures
the tune’s initialization routine as well as playback activity.

## RSID files

RSID expects a C64 environment, not a PSID player shim. Provide real ROMs via
`C64Config.roms` for meaningful compatibility work. The `--allow-hle-rsid`
option permits supplied HLE stubs for exploration only; it is deliberately not
evidence of real-hardware correctness.
