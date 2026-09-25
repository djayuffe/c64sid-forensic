# Development and release checks

Run from the repository root with Python 3.10 or newer:

```bash
/opt/local/bin/python3.10 -m compileall -q .
PYTHONPATH=.. /opt/local/bin/python3.10 -m unittest discover -s tests -v
ruff check --select F .
git diff --check
```

Build and inspect a wheel before publishing:

```bash
rm -rf build
python3 -m pip wheel --no-deps --wheel-dir /tmp/c64sid-wheel .
unzip -Z1 /tmp/c64sid-wheel/*.whl
```

The wheel must contain source modules and distribution metadata only—never
`__pycache__`, `.pyc`, `build/`, local WAV output, or forensic capture files.
Install it into an isolated target and check its version before tagging.

`setuptools` can reuse a local `build/` directory, so removing that generated
directory before a release build is required even when cache paths are ignored.

The regression suite covers parser rejection cases, C64 system stepping, VIC
DMA cycle progress, short PSID WAV rendering, and SID-PRO export validation.
