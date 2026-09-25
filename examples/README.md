# Runnable examples

Run the commands from the repository root after installing the package, or set
`PYTHONPATH=..` while working directly from this checkout.

## Self-contained smoke tune

This creates a valid but intentionally silent PSID file. It is safe to use in
automation because it contains no third-party music.

```bash
python3 examples/create_minimal_psid.py
python3 -m sid minimal.sid --info
python3 -m sid minimal.sid --wav minimal.wav --seconds 1 --sidpro minimal.sidpro.json
```

The command produces a valid WAV and SID-PRO capture; silence is expected.

## Inspect a real SID file

```bash
python3 examples/inspect_sid.py /path/to/tune.sid
```

This parses metadata and reports the payload size without executing the tune.

## Render a real PSID file

```bash
python3 examples/render_sid.py /path/to/tune.sid output.wav
```

The renderer also writes `output.sidpro.json`. Use real C64 ROM images for
meaningful RSID compatibility work; HLE is an exploratory fallback only.
