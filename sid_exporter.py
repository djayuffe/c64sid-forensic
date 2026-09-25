from __future__ import annotations

"""
SID-PRO Forensic Export Format (V6) - JSON writer/reader helpers.

This replaces the legacy "SID-PRO-FORENSIC-V6" object layout with the official
SID-PRO V6 spec described in SIDPRO_FORMAT.md.

Key properties:
- bus_cycles: float64le, one entry per SID register write (absolute cycle index)
- bus_events: uint8 triplets [chip, reg(0..28), value]
- ram_initial / ram_final: raw 64KB snapshots
- telemetry: frame-rate snapshots of SID internal state (osc/env/filter)

Binary payloads are base64-encoded zlib-compressed blobs by default and are
protected with SHA-256 checksums of the *uncompressed* raw byte payloads.
"""

import base64
import hashlib
import json
import time
import zlib
from typing import IO, Any, Dict, Iterable, Optional, Sequence

from .logger import SystemLogger


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64_chunks(data: bytes, chunk_size: int = 256 * 1024) -> Iterable[str]:
    """Yield base64 chunks as ASCII strings (no newlines)."""
    for i in range(0, len(data), chunk_size):
        yield base64.b64encode(data[i:i + chunk_size]).decode('ascii')


def _write_b64_string(fp: IO[str], data: bytes) -> None:
    fp.write('"')
    if data:
        for ch in _b64_chunks(data):
            fp.write(ch)
    fp.write('"')


def _compress(data: bytes, compression: str) -> bytes:
    if compression == 'none':
        return data
    if compression != 'zlib':
        raise ValueError(f"Unsupported compression: {compression}")
    # level 9 for maximal reduction; deterministic across runs.
    return zlib.compress(data, 9)


class SidExporter:
    """SID-PRO V6 export writer.

    Kept as "SidExporter" for compatibility with existing code.
    """
    FORMAT_VERSION = "6.0.0"
    DEFAULT_GENERATOR = "c64sid_py_forensic"

    @staticmethod
    def write_sidpro_json(
        fp: IO[str],
        *,
        clock_type: int,
        clock_hz: int,
        frame_rate: float,
        sid_models: Sequence[int],
        sid_addresses: Sequence[int],
        bus_cycles_f64le: bytes,
        bus_events_u8: bytes,
        ram_initial: bytes,
        ram_final: bytes,
        telemetry_frames: Sequence[Dict[str, Any]],
        generator: str = DEFAULT_GENERATOR,
        compression: str = "zlib",
        analysis: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Stream a SID-PRO V6 forensic JSON export to `fp`."""

        SystemLogger.log('SidExporter', 'Writing SID-PRO V6 forensic export...', 'info')

        sid_chips = int(len(sid_addresses))
        if len(sid_models) != sid_chips:
            raise ValueError("sid_models length must match sid_addresses length")

        # Validate sizes
        if len(ram_initial) != 65536 or len(ram_final) != 65536:
            raise ValueError("RAM snapshots must be exactly 65536 bytes")

        # bus_events is triplets; count is triplets count
        if len(bus_events_u8) % 3 != 0:
            raise ValueError("bus_events_u8 length must be a multiple of 3")
        events_count = len(bus_events_u8) // 3

        if len(bus_cycles_f64le) != events_count * 8:
            raise ValueError("bus_cycles_f64le byte length must be 8 * bus_events_count")

        # Checksums are on uncompressed payloads
        checksums = {
            "bus_cycles": _sha256_hex(bus_cycles_f64le),
            "bus_events": _sha256_hex(bus_events_u8),
            "ram_initial": _sha256_hex(ram_initial),
            "ram_final": _sha256_hex(ram_final),
        }

        cfg = {
            "clock_type": int(clock_type) & 0xFF,  # 0=PAL,1=NTSC
            "clock_hz": int(clock_hz),
            "frame_rate": float(frame_rate),
            "sid_chips": sid_chips,
            "sid_models": [int(x) & 0xFF for x in sid_models],
            "sid_addresses": [int(x) for x in sid_addresses],
        }

        meta = {
            "format_version": SidExporter.FORMAT_VERSION,
            "created": int(time.time()),
            "generator": str(generator),
            "config": cfg,
            "compression": str(compression),
            "checksums": checksums,
        }

        # Compress blobs
        bus_cycles_blob = _compress(bus_cycles_f64le, compression)
        bus_events_blob = _compress(bus_events_u8, compression)
        ram_init_blob = _compress(ram_initial, compression)
        ram_final_blob = _compress(ram_final, compression)

        # Stream JSON (avoid huge intermediate strings for base64)
        fp.write('{')

        fp.write('"metadata":')
        fp.write(json.dumps(meta, separators=(',', ':')))

        fp.write(',"binary":{')

        fp.write('"bus_cycles":{')
        fp.write('"encoding":"float64le","count":')
        fp.write(str(events_count))
        fp.write(',"data":')
        _write_b64_string(fp, bus_cycles_blob)
        fp.write('}')

        fp.write(',"bus_events":{')
        fp.write('"encoding":"uint8_triplets","count":')
        fp.write(str(events_count))
        fp.write(',"data":')
        _write_b64_string(fp, bus_events_blob)
        fp.write('}')

        fp.write(',"ram_initial":{')
        fp.write('"size":65536,"data":')
        _write_b64_string(fp, ram_init_blob)
        fp.write('}')

        fp.write(',"ram_final":{')
        fp.write('"size":65536,"data":')
        _write_b64_string(fp, ram_final_blob)
        fp.write('}')

        fp.write('}')

        fp.write(',"telemetry":{')
        fp.write('"frame_rate":')
        fp.write(json.dumps(float(frame_rate), separators=(',', ':')))
        fp.write(',"frames":')
        fp.write(json.dumps(list(telemetry_frames), separators=(',', ':')))
        fp.write('}')

        fp.write(',"analysis":')
        fp.write(json.dumps(analysis if analysis is not None else {}, separators=(',', ':')))

        fp.write('}')

    # Backwards-compatible name used by older code
    @staticmethod
    def write_json(*args: Any, **kwargs: Any) -> None:
        return SidExporter.write_sidpro_json(*args, **kwargs)

    # --- Reader helpers (optional, but useful for tests/tools) ---
    @staticmethod
    def decode_blob(b64: str, compression: str) -> bytes:
        raw = base64.b64decode(b64.encode('ascii')) if b64 else b""
        if compression == 'none':
            return raw
        if compression != 'zlib':
            raise ValueError(f"Unsupported compression: {compression}")
        return zlib.decompress(raw)

    @staticmethod
    def validate_sidpro(obj: Dict[str, Any]) -> None:
        """Validate a loaded SID-PRO V6 JSON object. Raises ValueError on issues."""
        if not isinstance(obj, dict):
            raise ValueError("Top-level must be an object")

        meta = obj.get("metadata")
        if not isinstance(meta, dict):
            raise ValueError("metadata missing/invalid")

        if str(meta.get("format_version")) != SidExporter.FORMAT_VERSION:
            raise ValueError("Unsupported format_version")

        compression = str(meta.get("compression", "zlib"))
        checksums = meta.get("checksums", {})
        if not isinstance(checksums, dict):
            raise ValueError("metadata.checksums missing/invalid")

        binary = obj.get("binary")
        if not isinstance(binary, dict):
            raise ValueError("binary missing/invalid")

        def _get_bin(name: str) -> Dict[str, Any]:
            d = binary.get(name)
            if not isinstance(d, dict):
                raise ValueError(f"binary.{name} missing/invalid")
            return d

        bus_cycles = _get_bin("bus_cycles")
        bus_events = _get_bin("bus_events")
        ram_initial = _get_bin("ram_initial")
        ram_final = _get_bin("ram_final")

        if bus_cycles.get("encoding") != "float64le":
            raise ValueError("bus_cycles.encoding must be float64le")
        if bus_events.get("encoding") != "uint8_triplets":
            raise ValueError("bus_events.encoding must be uint8_triplets")

        count = int(bus_cycles.get("count", -1))
        if count != int(bus_events.get("count", -2)):
            raise ValueError("bus_cycles.count must match bus_events.count")

        ram_sz_i = int(ram_initial.get("size", -1))
        ram_sz_f = int(ram_final.get("size", -1))
        if ram_sz_i != 65536 or ram_sz_f != 65536:
            raise ValueError("ram sizes must be 65536")

        # Decode and checksum
        cycles_raw = SidExporter.decode_blob(str(bus_cycles.get("data", "")), compression)
        events_raw = SidExporter.decode_blob(str(bus_events.get("data", "")), compression)
        ram_i_raw = SidExporter.decode_blob(str(ram_initial.get("data", "")), compression)
        ram_f_raw = SidExporter.decode_blob(str(ram_final.get("data", "")), compression)

        if len(events_raw) != count * 3:
            raise ValueError("Decoded bus_events length mismatch")
        if len(cycles_raw) != count * 8:
            raise ValueError("Decoded bus_cycles length mismatch")
        if len(ram_i_raw) != 65536 or len(ram_f_raw) != 65536:
            raise ValueError("Decoded RAM length mismatch")

        if checksums.get("bus_cycles") and checksums["bus_cycles"] != _sha256_hex(cycles_raw):
            raise ValueError("bus_cycles checksum mismatch")
        if checksums.get("bus_events") and checksums["bus_events"] != _sha256_hex(events_raw):
            raise ValueError("bus_events checksum mismatch")
        if checksums.get("ram_initial") and checksums["ram_initial"] != _sha256_hex(ram_i_raw):
            raise ValueError("ram_initial checksum mismatch")
        if checksums.get("ram_final") and checksums["ram_final"] != _sha256_hex(ram_f_raw):
            raise ValueError("ram_final checksum mismatch")
