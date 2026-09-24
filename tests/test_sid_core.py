from __future__ import annotations

import unittest

from sid.sid_parser import parse_sid_header
from sid.vic_dma import VicDmaEnhanced
from sid.vic_ii import VicII


def psid(*, offset: int = 0x7C, songs: int = 1, start_song: int = 1) -> bytes:
    header = bytearray(offset)
    header[:4] = b"PSID"
    header[4:6] = (2).to_bytes(2, "big")
    header[6:8] = offset.to_bytes(2, "big")
    header[8:10] = (0x1000).to_bytes(2, "big")
    header[10:12] = (0x1000).to_bytes(2, "big")
    header[12:14] = (0x1003).to_bytes(2, "big")
    header[14:16] = songs.to_bytes(2, "big")
    header[16:18] = start_song.to_bytes(2, "big")
    header[0x16:0x1A] = b"Test"
    return bytes(header) + b"\x60"


class SidParserTests(unittest.TestCase):
    def test_valid_header_and_payload(self) -> None:
        header, payload = parse_sid_header(psid())
        self.assertEqual(header.title, "Test")
        self.assertEqual(header.loadAddress, 0x1000)
        self.assertEqual(payload, b"\x60")

    def test_rejects_invalid_data_offset(self) -> None:
        with self.assertRaises(ValueError):
            parse_sid_header(psid(offset=0x76)[:0x70])

    def test_rejects_invalid_song_selection(self) -> None:
        with self.assertRaises(ValueError):
            parse_sid_header(psid(songs=1, start_song=2))


class VicDmaTests(unittest.TestCase):
    def test_badline_stalls_cpu(self) -> None:
        vic = VicII()
        vic.reset()
        # Reset uses $D011=$1B, so a badline must match YSCROLL=3.
        vic.rasterLine = 0x33
        vic.cycleCounter = 15
        self.assertTrue(VicDmaEnhanced().steal_info(vic).steal)


if __name__ == "__main__":
    unittest.main()
