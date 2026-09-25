# API guide

## Header parsing

```python
from sid import parse_sid_header

header, program = parse_sid_header(open("tune.sid", "rb").read())
```

`header` is a `SidHeader` dataclass. Its most useful fields are `magic`,
`version`, `loadAddress`, `initAddress`, `playAddress`, `songs`, `startSong`,
`isNtsc`, `sidModels`, and `sidAddresses`. `program` is the payload ready to
load at `loadAddress`. Invalid magic, unsupported versions, impossible song
selection, incomplete headers, and reserved extra-SID addresses raise
`ValueError`.

## PlaybackCoordinator

```python
from sid.playback import PlaybackCoordinator

player = PlaybackCoordinator()
player.load_sid_bytes(raw_sid, song=2)  # one-based; omit for the header default
result = player.render_to_wav("output.wav", seconds=30, sample_rate=44100)
```

`PlaybackResult` contains `wav_path`, `samples`, and `frames_rendered`. The
allowed sample-rate range is 8,000–96,000 Hz. `load_sid_bytes()` must succeed
before rendering. For a SID-PRO file, call `enable_forensic_dump(path)` before
loading.

## Configuration

`C64Config` configures HLE policy, optional BASIC/KERNAL/CHARGEN ROM bytes,
SID board/filter parameters, deterministic DSP, and tracing. Keep
`allowHleForRsid=False` unless an explicitly non-faithful RSID experiment is
intended.

## Low-level execution

`C64System.step(cycles)` advances shared PHI2 cycles. `C64System.call(addr,
a=0, x=0, y=0)` performs a host-side subroutine call and stops at the outer
`RTS` or the configured safety bound. These APIs are intended for tooling and
tests; ordinary rendering should use `PlaybackCoordinator`.
