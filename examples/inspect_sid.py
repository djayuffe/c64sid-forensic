"""Print safe, parsed metadata without running the tune."""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from sid import parse_sid_header


header, payload = parse_sid_header(Path(sys.argv[1]).read_bytes())
print(asdict(header))
print(f"program bytes: {len(payload)}")
