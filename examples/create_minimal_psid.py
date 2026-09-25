"""Create a minimal, valid PSID file for CLI and renderer smoke tests.

The generated tune has an RTS-only init/play routine, so it is silent. It is
useful for validating parsing, execution, WAV creation, and SID-PRO export
without redistributing third-party music.
"""

from __future__ import annotations

from pathlib import Path


def make_psid() -> bytes:
    header = bytearray(0x7C)
    header[:4] = b"PSID"
    header[4:6] = (2).to_bytes(2, "big")
    header[6:8] = (0x7C).to_bytes(2, "big")
    header[8:10] = (0x1000).to_bytes(2, "big")  # load
    header[10:12] = (0x1000).to_bytes(2, "big")  # init
    header[12:14] = (0x1003).to_bytes(2, "big")  # play
    header[14:16] = (1).to_bytes(2, "big")       # songs
    header[16:18] = (1).to_bytes(2, "big")       # start song
    header[0x16:0x36] = b"C64SID Forensic smoke test".ljust(32, b"\0")
    return bytes(header) + b"\x60\xea\xea\x60"  # RTS; NOP; NOP; RTS


if __name__ == "__main__":
    path = Path("minimal.sid")
    path.write_bytes(make_psid())
    print(f"Wrote {path.resolve()}")
