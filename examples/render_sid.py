"""Render a PSID file and optionally emit a SID-PRO V6 forensic capture."""

from __future__ import annotations

import sys
from pathlib import Path

from sid.playback import PlaybackCoordinator


source = Path(sys.argv[1])
output = Path(sys.argv[2])
player = PlaybackCoordinator()
player.enable_forensic_dump(str(output.with_suffix(".sidpro.json")))
player.load_sid_bytes(source.read_bytes())
result = player.render_to_wav(str(output), seconds=30)
print(result)
