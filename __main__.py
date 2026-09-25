"""Command-line entry point for inspecting and rendering SID files."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from .playback import PlaybackCoordinator
from .sid_parser import parse_sid_header
from .sid_types import C64Config


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or render a PSID/RSID file.")
    parser.add_argument("input", type=Path, help="input .sid file")
    parser.add_argument("--info", action="store_true", help="print parsed header only")
    parser.add_argument("--wav", type=Path, help="write mono 16-bit WAV")
    parser.add_argument("--seconds", type=float, default=30.0, help="render duration (default: 30)")
    parser.add_argument("--sample-rate", type=int, default=44100, help="WAV sample rate (default: 44100)")
    parser.add_argument("--sidpro", type=Path, help="write SID-PRO V6 JSON evidence")
    parser.add_argument("--allow-hle-rsid", action="store_true", help="allow non-faithful HLE ROM stubs for RSID")
    args = parser.parse_args()

    raw = args.input.read_bytes()
    header, _ = parse_sid_header(raw)
    if args.info or args.wav is None:
        for key, value in asdict(header).items():
            print(f"{key}: {value}")
        if args.wav is None:
            return 0

    config = C64Config(allowHleForRsid=args.allow_hle_rsid)
    player = PlaybackCoordinator(config)
    if args.sidpro:
        player.enable_forensic_dump(str(args.sidpro))
    player.load_sid_bytes(raw)
    result = player.render_to_wav(str(args.wav), args.seconds, args.sample_rate)
    print(f"Wrote {result.wav_path}: {result.samples} samples, {result.frames_rendered} play calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
