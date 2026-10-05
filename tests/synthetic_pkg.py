"""Builders for fake PS4/PS5 package files, used by the test suite.

These produce containers with real structure -- correct magic, a correctly encoded
``param.sfo`` table, a plausible content ID, a ``param.json`` blob -- without any
actual game data. That lets the parser be tested against the formats it claims to
read rather than against a mock of itself.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any, Dict

SFO_MAGIC = b"\x00PSF"
PS4_MAGIC = b"\x7fCNT"
PS5_MAGIC = b"\x7fFIH"

FMT_UTF8 = 0x0204
FMT_INT32 = 0x0404


def build_sfo(entries: Dict[str, Any]) -> bytes:
    """Encode a ``param.sfo`` from a plain dict of str/int values."""
    keys = list(entries.keys())

    key_blob = b""
    key_offsets = []
    for key in keys:
        key_offsets.append(len(key_blob))
        key_blob += key.encode("utf-8") + b"\x00"
    # The key table is padded to a 4-byte boundary in real files.
    while len(key_blob) % 4:
        key_blob += b"\x00"

    data_blob = b""
    index = b""
    for position, key in enumerate(keys):
        value = entries[key]
        data_offset = len(data_blob)
        if isinstance(value, int):
            encoded = struct.pack("<I", value)
            length = 4
            max_length = 4
            fmt = FMT_INT32
        else:
            encoded = str(value).encode("utf-8") + b"\x00"
            length = len(encoded)
            max_length = length + ((4 - length % 4) % 4)
            encoded = encoded + b"\x00" * (max_length - length)
            fmt = FMT_UTF8
        data_blob += encoded
        index += struct.pack("<HHIII", key_offsets[position], fmt, length, max_length, data_offset)

    header_size = 20 + len(index)
    key_table_start = header_size
    data_table_start = key_table_start + len(key_blob)
    header = SFO_MAGIC + struct.pack("<IIII", 0x00000101, key_table_start, data_table_start, len(keys))
    return header + index + key_blob + data_blob


def build_ps4_pkg(
    path: Path,
    content_id: str = "UP0001-CUSA12345_00-0123456789ABCDEF",
    title: str = "Synthetic Test Game",
    version: str = "1.02",
    category: str = "gd",
    padding: int = 4096,
) -> Path:
    """Write a fake PS4 package with an embedded param.sfo."""
    sfo = build_sfo(
        {
            "APP_VER": version,
            "CATEGORY": category,
            "CONTENT_ID": content_id,
            "TITLE": title,
            "TITLE_ID": content_id.split("-")[1].split("_")[0],
            "VERSION": version,
        }
    )

    blob = bytearray(PS4_MAGIC)
    blob += b"\x00" * (0x40 - len(blob))
    blob += content_id.encode("ascii").ljust(0x24, b"\x00")
    blob += b"\x00" * 0x40
    blob += sfo
    blob += b"\x00" * padding

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(blob))
    return path


def build_ps5_pkg(
    path: Path,
    title_id: str = "PPSA54321",
    title: str = "Synthetic PS5 Title",
    version: str = "01.004.000",
    padding: int = 2048,
) -> Path:
    """Write a fake PS5 package carrying a param.json instead of an SFO."""
    param = {
        "applicationCategoryType": 0,
        "titleId": title_id,
        "contentVersion": version,
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": title}},
        "titleName": title,
    }

    blob = bytearray(PS5_MAGIC)
    blob += b"\x00" * (0x40 - len(blob))
    blob += ("EP9000-" + title_id + "_00-FEDCBA9876543210").encode("ascii").ljust(0x24, b"\x00")
    blob += b"\x00" * 0x20
    blob += json.dumps(param).encode("utf-8")
    blob += b"\x00" * padding

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(blob))
    return path


def build_garbage_pkg(path: Path, size: int = 2048) -> Path:
    """A file with the right extension and nothing else recognisable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"NOTAPKG!" + b"\x5a" * size)
    return path
