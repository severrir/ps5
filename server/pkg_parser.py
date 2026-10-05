"""Offline metadata extraction from PS4/PS5 ``.pkg`` files.

There is no official public specification for these containers, and header field
offsets differ between package revisions. Guessing an offset and presenting the
result as fact is worse than admitting uncertainty, so this parser is layered and
always reports which layer produced the answer:

1. **Magic** -- ``\\x7fCNT`` (PS4) or ``\\x7fFIH`` (PS5). Anything else is handled
   as an unrecognised container rather than an error.
2. **Embedded ``param.sfo``** -- a well-specified key/value table. When found and
   structurally sane it gives the real ``TITLE``, ``TITLE_ID``, ``APP_VER`` and
   ``CATEGORY``. This is the best source and is used when available.
3. **Embedded ``param.json``** -- what PS5 packages carry instead of an SFO.
   Scraped with bounded regexes for ``titleId`` / ``titleName`` / version.
4. **Content ID scan** -- the canonical ``UP0001-CUSA00001_00-XXXXXXXXXXXXXXXX``
   shape is distinctive enough to find by pattern and yields a reliable title ID.
5. **Filename** -- last resort, always succeeds.

Every result carries ``metadata_source`` and ``warnings`` so the UI can show a
title as derived rather than authoritative. Nothing here touches the network.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional

PS4_MAGIC = b"\x7fCNT"
PS5_MAGIC = b"\x7fFIH"
SFO_MAGIC = b"\x00PSF"

# UP0001-CUSA00001_00-0123456789ABCDEF  (region-publisher, title id, entitlement)
CONTENT_ID_RE = re.compile(rb"[A-Z]{2}\d{4}-([A-Z]{4}\d{5})_\d{2}-[0-9A-Z]{16}")
# Bare title IDs as a weaker fallback: CUSAxxxxx (PS4) / PPSAxxxxx (PS5).
TITLE_ID_RE = re.compile(rb"\b((?:CUSA|PPSA|PLAS|FLAS)\d{5})\b")

JSON_TITLE_ID_RE = re.compile(rb'"titleId"\s*:\s*"([0-9A-Za-z_\-]{4,24})"')
JSON_TITLE_NAME_RE = re.compile(rb'"titleName"\s*:\s*"((?:[^"\\]|\\.){1,200})"')
JSON_VERSION_RE = re.compile(rb'"(?:contentVersion|masterVersion|applicationVersion)"\s*:\s*"([^"]{1,32})"')

# How far into the file we look for an embedded SFO/JSON blob. In practice these
# live in the first few hundred KB; 24 MiB is generous and still bounded so a
# 100 GB package never costs more than a couple of reads.
SCAN_LIMIT_BYTES = 24 * 1024 * 1024
SCAN_CHUNK_BYTES = 1024 * 1024
# Overlap between chunks so a magic/pattern straddling a boundary is still found.
SCAN_OVERLAP_BYTES = 4096

# param.sfo CATEGORY values. Anything unlisted is surfaced verbatim.
CATEGORY_LABELS = {
    "gd": "Game",
    "gde": "Game",
    "gda": "App",
    "gp": "Update",
    "ac": "DLC",
    "gc": "DLC",
    "sd": "Save Data",
    "la": "App",
    "bd": "Disc",
}

SFO_FMT_UTF8_SPECIAL = 0x0004
SFO_FMT_UTF8 = 0x0204
SFO_FMT_INT32 = 0x0404


# --------------------------------------------------------------------------- #
# param.sfo
# --------------------------------------------------------------------------- #


def parse_sfo(blob: bytes) -> Dict[str, Any]:
    """Parse a ``param.sfo`` byte blob into a plain dict.

    Returns ``{}`` when the blob is not a structurally valid SFO. The bounds
    checks matter: the blob was located by scanning for a 4-byte magic, so false
    positives are expected and must fail quietly.
    """
    if len(blob) < 20 or blob[:4] != SFO_MAGIC:
        return {}
    try:
        _version, key_table, data_table, entry_count = struct.unpack_from("<IIII", blob, 4)
    except struct.error:
        return {}

    # Sanity: a real SFO has a handful of entries and tables inside the blob.
    if not (0 < entry_count <= 512):
        return {}
    index_end = 20 + entry_count * 16
    if not (index_end <= key_table <= data_table <= len(blob)):
        return {}

    result: Dict[str, Any] = {}
    for index in range(entry_count):
        offset = 20 + index * 16
        try:
            key_offset, fmt, length, _max_length, data_offset = struct.unpack_from(
                "<HHIII", blob, offset
            )
        except struct.error:
            break

        key_start = key_table + key_offset
        if key_start >= len(blob):
            continue
        key_end = blob.find(b"\x00", key_start)
        if key_end == -1:
            continue
        key = blob[key_start:key_end].decode("utf-8", "replace")

        value_start = data_table + data_offset
        value_end = value_start + length
        if value_start >= len(blob) or value_end > len(blob) or length > 1 << 20:
            continue
        chunk = blob[value_start:value_end]

        if fmt == SFO_FMT_INT32:
            if len(chunk) >= 4:
                result[key] = struct.unpack_from("<I", chunk, 0)[0]
        elif fmt in (SFO_FMT_UTF8, SFO_FMT_UTF8_SPECIAL):
            result[key] = chunk.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        else:
            result[key] = chunk.hex()

    return result


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #


def _iter_chunks(handle, limit: int):
    """Yield ``(absolute_offset, chunk)`` with overlap, up to ``limit`` bytes."""
    position = 0
    carry = b""
    carry_offset = 0
    while position < limit:
        chunk = handle.read(min(SCAN_CHUNK_BYTES, limit - position))
        if not chunk:
            break
        if carry:
            yield carry_offset, carry + chunk
        else:
            yield position, chunk
        carry_offset = position + len(chunk) - SCAN_OVERLAP_BYTES
        carry = chunk[-SCAN_OVERLAP_BYTES:] if len(chunk) >= SCAN_OVERLAP_BYTES else chunk
        if carry_offset < 0:
            carry_offset = 0
            carry = b""
        position += len(chunk)


def _scan(path: Path, size: int) -> Dict[str, Any]:
    """One pass over the head of the file collecting every usable signal."""
    found: Dict[str, Any] = {"sfo": {}, "json": {}, "content_id": None, "title_id": None}
    limit = min(size, SCAN_LIMIT_BYTES)

    with open(path, "rb", buffering=0) as handle:
        for base, chunk in _iter_chunks(handle, limit):
            if not found["sfo"]:
                start = chunk.find(SFO_MAGIC)
                while start != -1:
                    absolute = base + start
                    # Read a bounded window around the candidate and try to parse.
                    handle_position = handle.tell()
                    try:
                        handle.seek(absolute)
                        candidate = handle.read(256 * 1024)
                    finally:
                        handle.seek(handle_position)
                    parsed = parse_sfo(candidate)
                    if parsed:
                        found["sfo"] = parsed
                        break
                    start = chunk.find(SFO_MAGIC, start + 1)

            if not found["json"]:
                title_id = JSON_TITLE_ID_RE.search(chunk)
                title_name = JSON_TITLE_NAME_RE.search(chunk)
                if title_id or title_name:
                    payload: Dict[str, Any] = {}
                    if title_id:
                        payload["titleId"] = title_id.group(1).decode("utf-8", "replace")
                    if title_name:
                        raw_name = title_name.group(1).decode("utf-8", "replace")
                        try:
                            payload["titleName"] = json.loads('"' + raw_name + '"')
                        except ValueError:
                            payload["titleName"] = raw_name
                    version = JSON_VERSION_RE.search(chunk)
                    if version:
                        payload["version"] = version.group(1).decode("utf-8", "replace")
                    found["json"] = payload

            if not found["content_id"]:
                match = CONTENT_ID_RE.search(chunk)
                if match:
                    found["content_id"] = match.group(0).decode("ascii", "replace")
                    found["title_id"] = match.group(1).decode("ascii", "replace")

            if not found["title_id"]:
                match = TITLE_ID_RE.search(chunk)
                if match:
                    found["title_id"] = match.group(1).decode("ascii", "replace")

            if found["sfo"] and found["content_id"]:
                break

    return found


def _clean_title(value: str) -> str:
    """Collapse whitespace and strip control characters from a title string."""
    cleaned = "".join(char for char in value if char.isprintable())
    return re.sub(r"\s+", " ", cleaned).strip()


def title_from_filename(path: Path) -> str:
    """Human-ish title derived from a messy filename.

    Drops the extension, strips bracketed tags and the usual scene decorations,
    and collapses separators. Always returns something non-empty.
    """
    stem = path.stem
    stem = re.sub(r"[\[(][^\])]*[\])]", " ", stem)
    # Underscores go first: "_" is a word character, so \bPS4\b would never match
    # inside "..._PS4_FPKG" and the tags would survive into the title.
    stem = stem.replace("_", " ")
    stem = re.sub(r"\b(?:CUSA|PPSA|PLAS|FLAS)\d{5}\b", " ", stem, flags=re.IGNORECASE)
    # Version numbers are matched while dots are still intact.
    stem = re.sub(r"\bv?\d+\.\d+(?:\.\d+)?\b", " ", stem)
    stem = stem.replace(".", " ").replace("-", " ")
    stem = re.sub(
        r"\b(?:PS4|PS5|FPKG|PKG|UPDATE|PATCH|DLC|USA|EUR|JPN|MULTI\d*)\b", " ", stem, flags=re.IGNORECASE
    )
    stem = re.sub(r"\s+", " ", stem).strip(" -")
    return stem or path.stem


def detect_magic(path: Path) -> str:
    """``"ps4"``, ``"ps5"`` or ``"unknown"`` from the first four bytes."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        return "unknown"
    if head == PS4_MAGIC:
        return "ps4"
    if head == PS5_MAGIC:
        return "ps5"
    return "unknown"


def parse(path: Path) -> Dict[str, Any]:
    """Extract everything we can about one package file.

    Never raises for a malformed or unreadable package: a result is always
    returned, with the problem recorded in ``warnings``.
    """
    path = Path(path)
    warnings: List[str] = []
    sources: List[str] = []

    try:
        stat = path.stat()
        size = int(stat.st_size)
        mtime = float(stat.st_mtime)
    except OSError as exc:
        return {
            "path": str(path),
            "filename": path.name,
            "title": title_from_filename(path),
            "title_id": None,
            "content_id": None,
            "version": None,
            "category": "Unknown",
            "platform": "unknown",
            "size_bytes": 0,
            "mtime": 0.0,
            "metadata_source": ["filename"],
            "warnings": [f"unreadable: {exc}"],
        }

    platform = detect_magic(path)
    if platform == "unknown":
        warnings.append("Unrecognised container magic; metadata is filename-derived.")

    try:
        signals = _scan(path, size)
    except OSError as exc:
        signals = {"sfo": {}, "json": {}, "content_id": None, "title_id": None}
        warnings.append(f"Scan failed: {exc}")

    sfo: Dict[str, Any] = signals.get("sfo") or {}
    pkg_json: Dict[str, Any] = signals.get("json") or {}

    title: Optional[str] = None
    title_id: Optional[str] = None
    version: Optional[str] = None
    category_raw: Optional[str] = None

    if sfo:
        sources.append("param.sfo")
        if sfo.get("TITLE"):
            title = _clean_title(str(sfo["TITLE"]))
        title_id = str(sfo.get("TITLE_ID") or "") or None
        version = str(sfo.get("APP_VER") or sfo.get("VERSION") or "") or None
        category_raw = str(sfo.get("CATEGORY") or "") or None

    if pkg_json:
        sources.append("param.json")
        if not title and pkg_json.get("titleName"):
            title = _clean_title(str(pkg_json["titleName"]))
        title_id = title_id or pkg_json.get("titleId")
        version = version or pkg_json.get("version")

    if signals.get("content_id"):
        sources.append("content_id")
        title_id = title_id or signals.get("title_id")

    if not title_id and signals.get("title_id"):
        sources.append("title_id_scan")
        title_id = signals.get("title_id")

    if not title:
        sources.append("filename")
        title = title_from_filename(path)

    category = CATEGORY_LABELS.get((category_raw or "").lower(), None)
    if category is None:
        if category_raw:
            category = category_raw.upper()
        elif re.search(r"\b(update|patch)\b", path.name, re.IGNORECASE):
            category = "Update"
        elif re.search(r"\bdlc\b", path.name, re.IGNORECASE):
            category = "DLC"
        else:
            category = "Game" if platform in ("ps4", "ps5") else "Unknown"

    return {
        "path": str(path),
        "filename": path.name,
        "title": title,
        "title_id": title_id,
        "content_id": signals.get("content_id"),
        "version": version,
        "category": category,
        "category_raw": category_raw,
        "platform": platform,
        "size_bytes": size,
        "mtime": mtime,
        "metadata_source": sources or ["filename"],
        "warnings": warnings,
    }


def format_size(num_bytes: int) -> str:
    """Human-readable size used by the API and mirrored by the UI formatter."""
    value = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{value:.0f} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"
