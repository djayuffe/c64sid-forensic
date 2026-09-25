# Changelog

All notable project-facing changes are documented here.

## 1.1.1 — 2026-09-25

- Prevent generated Python bytecode and cache directories from being included
  in source builds and wheels.
- Verified a clean wheel, repository integrity, static checks, and the complete
  regression suite.

## 1.1.0 — 2026-09-25

- Repaired `C64System` DMA and public execution-method placement.
- Ensure every VIC DMA stall consumes a shared PHI2 cycle.
- Initialize SID oscillator/envelope readback latches at construction.
- Tightened PSID v2+ header and multi-SID address validation.
- Added CLI inspection/rendering, executable examples, architecture notes, and
  end-to-end WAV regression coverage.

## 1.0.0 — 2026-09-25

- First standalone release of the C64 SID analysis and playback core.
