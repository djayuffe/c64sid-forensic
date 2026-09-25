from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from sid.sid_parser import parse_sid_header
from sid.c64_system import C64System
from sid.vic_dma import VicDmaEnhanced
from sid.vic_ii import VicII
from sid.playback import PlaybackCoordinator
from sid.sid_exporter import SidExporter


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

    def test_rejects_v2_header_with_v1_data_offset(self) -> None:
        with self.assertRaises(ValueError):
            parse_sid_header(psid(offset=0x76))

    def test_ignores_reserved_second_sid_address(self) -> None:
        raw = bytearray(psid())
        raw[0x7A] = 0x80  # reserved $D800-$DDF0 range
        header, _ = parse_sid_header(bytes(raw))
        self.assertEqual(header.sidCount, 1)


class VicDmaTests(unittest.TestCase):
    def test_badline_stalls_cpu(self) -> None:
        vic = VicII()
        vic.reset()
        # Reset uses $D011=$1B, so a badline must match YSCROLL=3.
        vic.rasterLine = 0x33
        vic.cycleCounter = 15
        self.assertTrue(VicDmaEnhanced().steal_info(vic).steal)

    def test_system_constructs_and_steps(self) -> None:
        system = C64System()
        self.assertEqual(system.step(32), 32)
        self.assertGreater(system.stats.instructions, 0)

    def test_badline_stall_advances_time(self) -> None:
        system = C64System()
        system.memory.vic.reset()
        system.memory.vic.rasterLine = 0x33
        system.memory.vic.cycleCounter = 15
        before = system.stats.cpuCycles
        system._vic_steal_cycle()
        self.assertEqual(system.stats.cpuCycles, before + 1)


class PlaybackTests(unittest.TestCase):
    @staticmethod
    def _minimal_tune() -> bytes:
        raw = bytearray(psid())
        # Init RTS at $1000; play RTS at $1003.
        raw[-1:] = b"\x60\xea\xea\x60"
        return bytes(raw)

    def test_minimal_psid_renders_wav(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "render.wav"
            player = PlaybackCoordinator()
            player.load_sid_bytes(self._minimal_tune())
            result = player.render_to_wav(str(output), seconds=0.01, sample_rate=8000)
            self.assertEqual(result.samples, 80)
            self.assertEqual(output.stat().st_size, 44 + result.samples * 2)

    def test_forensic_export_validates(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dump = root / "capture.sidpro.json"
            player = PlaybackCoordinator()
            player.enable_forensic_dump(str(dump))
            player.load_sid_bytes(self._minimal_tune())
            player.render_to_wav(str(root / "render.wav"), seconds=0.01, sample_rate=8000)
            exported = json.loads(dump.read_text(encoding="utf-8"))
            SidExporter.validate_sidpro(exported)
            self.assertEqual(exported["metadata"]["format_version"], "6.0.0")

    def test_selects_valid_subsong_and_rejects_invalid_one(self) -> None:
        raw = bytearray(psid(songs=2, start_song=1))
        raw[-1:] = b"\x60\xea\xea\x60"
        player = PlaybackCoordinator()
        player.load_sid_bytes(bytes(raw), song=2)
        self.assertEqual(player.selected_song, 2)
        with self.assertRaises(ValueError):
            PlaybackCoordinator().load_sid_bytes(bytes(raw), song=3)


if __name__ == "__main__":
    unittest.main()
