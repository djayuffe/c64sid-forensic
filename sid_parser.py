from __future__ import annotations

from dataclasses import asdict
from typing import Tuple

from .logger import SystemLogger
from .sid_types import SidHeader


def _read_u16_be(b: bytes, off: int) -> int:
    return (b[off] << 8) | b[off + 1]


def _read_u32_be(b: bytes, off: int) -> int:
    return (b[off] << 24) | (b[off + 1] << 16) | (b[off + 2] << 8) | b[off + 3]


def _map_sid_address(id_byte: int) -> int:
    # Mirrors TS mapSidAddress
    if id_byte < 0x42 or id_byte > 0xFE or (id_byte & 1) != 0:
        return 0
    if 0x80 <= id_byte <= 0xDF:
        return 0
    return 0xD000 + (id_byte << 4)


def parse_sid_header(raw: bytes) -> Tuple[SidHeader, bytes]:
    if len(raw) < 0x76:
        raise ValueError('Invalid SID file: too short')

    magic = raw[0:4].decode('ascii', errors='replace')
    SystemLogger.log('SidParser', f'Parsing SID Header... Magic: {magic}', 'debug')

    if magic not in ('PSID', 'RSID'):
        SystemLogger.log('SidParser', f"Critical: Unknown Magic ID '{magic}'", 'error')
        raise ValueError('Invalid SID file: Magic ID mismatch (Expected PSID or RSID)')

    is_rsid = magic == 'RSID'
    version = _read_u16_be(raw, 4)
    data_offset = _read_u16_be(raw, 6)
    load_address = _read_u16_be(raw, 8)
    init_address = _read_u16_be(raw, 10)
    play_address = _read_u16_be(raw, 12)
    songs = _read_u16_be(raw, 14)
    start_song = _read_u16_be(raw, 16)
    speed = _read_u32_be(raw, 18)

    if version == 0 or version > 4:
        raise ValueError(f'Unsupported SID version: {version}')
    if data_offset < 0x76 or data_offset > len(raw):
        raise ValueError(f'Invalid SID data offset: {data_offset:#x}')
    if songs == 0 or start_song == 0 or start_song > songs:
        raise ValueError('Invalid SID song count or start song')

    if is_rsid:
        if load_address != 0:
            load_address = 0
        if play_address != 0:
            play_address = 0

    # iso-8859-1 matches TextDecoder('iso-8859-1')
    def dec(off: int) -> str:
        return raw[off:off + 0x20].decode('latin-1', errors='replace').replace('\x00', '').rstrip('\x00')

    title = dec(0x16)
    author = dec(0x36)
    released = dec(0x56)

    flags = 0
    if version >= 2 and len(raw) >= 0x78:
        flags = _read_u16_be(raw, 0x76)

    
    reloc_start_page = 0
    reloc_page_len = 0
    if version >= 2 and len(raw) >= 0x7C:
        # PSID v2+ relocation information: which C64 pages are occupied by the driver.
        # startPage==0 and pageLength==0 means "unknown / not provided".
        reloc_start_page = int(raw[0x78])
        reloc_page_len = int(raw[0x79])
    is_ntsc = False
    video_std = (flags >> 2) & 0x03

    if is_rsid or (version >= 2 and (flags & 0x0C) != 0):
        if video_std in (2, 3):
            is_ntsc = True

    c64_basic_flag = False
    if is_rsid:
        c64_basic_flag = (flags & 0x02) != 0

    sid_models = []
    sid_addresses = []

    model_bits1 = (flags >> 4) & 0x03
    sid_models.append('6581' if model_bits1 == 1 else '8580' if model_bits1 == 2 else 'UNKNOWN')
    sid_addresses.append(0xD400)

    if version >= 2 and len(raw) > 0x7A:
        model_bits2 = (flags >> 6) & 0x03
        addr_byte = raw[0x7A]
        if addr_byte >= 0x42 and (addr_byte & 1) == 0:
            sid_models.append('6581' if model_bits2 == 1 else '8580' if model_bits2 == 2 else sid_models[0])
            sid_addresses.append(_map_sid_address(addr_byte))

    if version >= 3 and len(raw) > 0x7B:
        model_bits3 = (flags >> 8) & 0x03
        addr_byte = raw[0x7B]
        if addr_byte >= 0x42 and (addr_byte & 1) == 0:
            sid_models.append('6581' if model_bits3 == 1 else '8580' if model_bits3 == 2 else sid_models[0])
            sid_addresses.append(_map_sid_address(addr_byte))

    pal_clock = 985_248
    ntsc_clock = 1_022_730
    clock_freq = ntsc_clock if is_ntsc else pal_clock

    memory_data = raw[data_offset:]
    if load_address == 0:
        if len(memory_data) < 2:
            raise ValueError('Invalid SID data: Too short')
        load_address = memory_data[0] | (memory_data[1] << 8)
        memory_data = memory_data[2:]

    if init_address == 0 and not (is_rsid and c64_basic_flag):
        init_address = load_address

    header = SidHeader(
        magic=magic,  # type: ignore
        version=version,
        dataOffset=data_offset,
        loadAddress=load_address,
        initAddress=init_address,
        playAddress=play_address,
        songs=songs,
        startSong=start_song,
        speed=speed,
        title=title,
        author=author,
        released=released,
        flags=flags,
        isNtsc=is_ntsc,
        clockFreq=clock_freq,
        model=sid_models[0],  # type: ignore
        sidCount=len(sid_models),
        sidModels=sid_models,  # type: ignore
        sidAddresses=sid_addresses,
        relocStartPage=reloc_start_page,
        relocPageLength=reloc_page_len,
        c64BasicFlag=c64_basic_flag,
    )

    return header, memory_data
