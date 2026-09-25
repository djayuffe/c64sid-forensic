# Architecture

## Execution path

```text
PSID / RSID bytes
      │
      ▼
sid_parser ──► SidHeader + program payload
      │
      ▼
PlaybackCoordinator
      │ installs ROMs / HLE, maps SID chips, loads program
      ▼
C64System ──► Cpu6502 ──► MemoryBank ──► SID / VIC-II / CIA
      │             │             │
      │             └─ VIC DMA bus arbitration ─┘
      ▼
WAV samples + optional SID-PRO V6 capture
```

## Module responsibilities

- `sid_parser.py` validates and decodes file headers. It does not execute tune
  code and is the safest choice for cataloguing files.
- `c64_system.py` owns the global PHI2 clock. CPU accesses and each VIC DMA
  stall advance the VIC, CIAs, SID chips, and global cycle count exactly once.
- `memory_bank.py` models 6510 port-controlled banking and routes I/O ranges.
- `cpu6502.py` provides documented and illegal opcode execution plus optional
  bus/write observers for forensic capture.
- `sid_chip.py` models register-visible SID behaviour and provides the sample
  mixer output; it is deterministic for a fixed configuration and input.
- `playback/` bridges the C64 execution cadence to output sample timing.

## Fidelity boundaries

The project is purpose-built for SID analysis. It does model badline and
sprite DMA *stalls*, but not complete VIC memory fetch/collision semantics.
Sprite-data DMA advances the shared bus clock using a stable placeholder read;
it must not be interpreted as a screen-memory-accurate sprite fetch. For RSID,
use dumped BASIC/KERNAL/CHARGEN ROMs when compatibility matters.

## Forensic captures

`enable_forensic_dump()` installs a cycle counter before reset, captures SID
register writes, and writes RAM snapshots plus telemetry after rendering. The
result describes this emulator configuration and must be accompanied by ROM,
model, and sample-rate provenance when used in a comparison workflow.
