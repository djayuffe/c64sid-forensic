# SID-PRO V6 export

The optional forensic export is JSON with four top-level objects:

| Key | Purpose |
| --- | --- |
| `metadata` | Format version, generator, C64/SID configuration, compression, checksums |
| `binary` | Base64-encoded binary capture payloads |
| `telemetry` | Frame-rate SID voice and filter snapshots |
| `analysis` | Caller-reserved analysis object; currently emitted as `{}` |

The current `metadata.format_version` is `6.0.0`. `binary.bus_cycles` stores
little-endian float64 cycle positions, while `binary.bus_events` stores
`[chip, register, value]` uint8 triplets. RAM blobs are exactly 65,536 bytes
after base64 decoding and decompression.

Payloads use the `metadata.compression` setting (`zlib` by default), and their
SHA-256 digests cover the uncompressed bytes. Validate a loaded export with:

```python
import json
from sid.sid_exporter import SidExporter

capture = json.load(open("tune.sidpro.json"))
SidExporter.validate_sidpro(capture)
```

Validation checks schema-critical fields, decoded lengths, event counts, and
checksums. It does not claim that the captured tune is hardware-faithful; ROM,
model, configuration, and version provenance still matter.
