"""Package library: directory scanning, metadata caching, and user overrides.

The library is the union of two sources:

* **Local packages** -- ``.pkg`` files under ``library.pkg_directories``, scanned
  on a background timer and on demand. Metadata comes from
  :mod:`server.pkg_parser` and is cached by (path, mtime, size) so a rescan of a
  large directory costs almost nothing when nothing changed.
* **Pasted URLs** -- direct links you add by hand. These are queued straight to
  the console without the hub hosting the bytes.

User-authored state (renames, favourites, tags, hidden flags, first-seen time)
lives in ``data/library.json`` keyed by a stable item id, so it survives rescans
and is never written back into the package files themselves.

No remote index, catalogue, or art service is consulted -- by design.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

from cachetools import TTLCache

from . import config, pkg_parser
from .logs import logger

SORT_KEYS = ("title", "size", "recent", "category")


def _item_id(seed: str) -> str:
    """Short, stable id for an item. Path- or URL-derived, never random."""
    return hashlib.sha1(seed.lower().encode("utf-8")).hexdigest()[:12]


class LibraryStore:
    """Persistent, user-authored overlay on top of the scanned library."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (config.DATA_DIR / "library.json")
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"overrides": {}, "urls": [], "version": 1}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("library.json unreadable (%s); starting fresh", exc)
            return
        if isinstance(loaded, dict):
            self._data["overrides"] = loaded.get("overrides") or {}
            self._data["urls"] = loaded.get("urls") or []

    def _flush(self) -> None:
        try:
            config.write_atomic(self.path, json.dumps(self._data, indent=1) + "\n")
        except OSError as exc:
            logger.error("Could not write library.json: %s", exc)

    # -- overrides ---------------------------------------------------------- #

    def override(self, item_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data["overrides"].get(item_id) or {})

    def set_override(self, item_id: str, **fields: Any) -> Dict[str, Any]:
        """Merge ``fields`` into an item's override record and persist."""
        with self._lock:
            current = dict(self._data["overrides"].get(item_id) or {})
            for key, value in fields.items():
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            if current:
                self._data["overrides"][item_id] = current
            else:
                self._data["overrides"].pop(item_id, None)
            self._flush()
            return current

    def note_first_seen(self, item_id: str, when: float) -> float:
        """Record when an item first appeared; returns the stored timestamp."""
        with self._lock:
            current = self._data["overrides"].setdefault(item_id, {})
            if "first_seen" not in current:
                current["first_seen"] = when
                self._flush()
            return float(current["first_seen"])

    # -- url entries -------------------------------------------------------- #

    def urls(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._data["urls"]]

    def add_url(self, url: str, title: Optional[str] = None) -> Dict[str, Any]:
        entry = {
            "id": _item_id(url),
            "url": url,
            "title": title or self.title_from_url(url),
            "added_at": time.time(),
        }
        with self._lock:
            for existing in self._data["urls"]:
                if existing.get("id") == entry["id"]:
                    return dict(existing)
            self._data["urls"].append(entry)
            self._flush()
        return entry

    def remove_url(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._data["urls"])
            self._data["urls"] = [e for e in self._data["urls"] if e.get("id") != item_id]
            changed = len(self._data["urls"]) != before
            if changed:
                self._flush()
            return changed

    @staticmethod
    def title_from_url(url: str) -> str:
        name = unquote(Path(urlparse(url).path).name) or url
        return pkg_parser.title_from_filename(Path(name))

    def clear(self) -> None:
        """Forget all overrides and URL entries (used by the wipe routine)."""
        with self._lock:
            self._data = {"overrides": {}, "urls": [], "version": 1}
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError:
                pass


class Library:
    """Scans the configured directories and exposes the merged item list."""

    def __init__(self, store: Optional[LibraryStore] = None) -> None:
        self.store = store or LibraryStore()
        self._lock = threading.RLock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._meta_cache: TTLCache = TTLCache(
            maxsize=int(config.get("library.max_items", 5000) or 5000),
            ttl=float(config.get("library.cache_ttl_seconds", 300) or 300),
        )
        self._last_scan: float = 0.0
        self._last_scan_duration: float = 0.0
        self._scanning = False
        self._scan_error: Optional[str] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        """Begin the background scan loop (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="library-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as exc:  # pragma: no cover - a scan must never kill the thread
                self._scan_error = str(exc)
                logger.exception("Library scan failed: %s", exc)
            interval = max(10, int(config.get("library.scan_interval_seconds", 60) or 60))
            self._wake.wait(interval)
            self._wake.clear()

    def request_rescan(self) -> None:
        """Ask the background loop to scan now."""
        self._wake.set()

    # -- scanning ----------------------------------------------------------- #

    def _iter_package_files(self) -> Iterable[Path]:
        extensions = tuple(
            str(ext).lower() if str(ext).startswith(".") else "." + str(ext).lower()
            for ext in (config.get("library.extensions", [".pkg"]) or [".pkg"])
        )
        recursive = bool(config.get("library.recursive", True))
        limit = int(config.get("library.max_items", 5000) or 5000)
        seen = 0

        for root in config.pkg_directories():
            if recursive:
                walker = os.walk(root)
            else:
                walker = [(str(root), [], [p.name for p in root.iterdir() if p.is_file()])]
            for dirpath, _dirnames, filenames in walker:
                for filename in filenames:
                    if not filename.lower().endswith(extensions):
                        continue
                    seen += 1
                    if seen > limit:
                        logger.warning("library.max_items (%d) reached; stopping scan", limit)
                        return
                    yield Path(dirpath) / filename

    def _metadata(self, path: Path) -> Dict[str, Any]:
        """Parsed metadata for one file, cached by identity + mtime + size."""
        try:
            stat = path.stat()
        except OSError:
            return pkg_parser.parse(path)
        key = (str(path).lower(), int(stat.st_mtime), int(stat.st_size))
        cached = self._meta_cache.get(key)
        if cached is not None:
            return cached
        parsed = pkg_parser.parse(path)
        self._meta_cache[key] = parsed
        return parsed

    def scan(self) -> Dict[str, Any]:
        """Rebuild the item index. Returns a short summary."""
        started = time.perf_counter()
        with self._lock:
            self._scanning = True
        try:
            found: Dict[str, Dict[str, Any]] = {}
            now = time.time()

            for path in self._iter_package_files():
                meta = self._metadata(path)
                item_id = _item_id(str(path))
                first_seen = self.store.note_first_seen(item_id, meta.get("mtime") or now)
                found[item_id] = self._compose_local(item_id, meta, first_seen)

            for entry in self.store.urls():
                found[entry["id"]] = self._compose_url(entry, now)

            self._scan_error = None
            with self._lock:
                self._items = found
                self._last_scan = now
                self._last_scan_duration = time.perf_counter() - started
            logger.info(
                "Library scan: %d item(s) in %.0f ms",
                len(found),
                self._last_scan_duration * 1000,
            )
            return {"items": len(found), "duration_ms": round(self._last_scan_duration * 1000)}
        finally:
            with self._lock:
                self._scanning = False

    def _compose_url(self, entry: Dict[str, Any], now: float) -> Dict[str, Any]:
        """Build the item view for a pasted link.

        Size is unknown by design: the console fetches the URL itself, so the hub
        never sees the bytes and must not guess a figure.
        """
        item_id = entry["id"]
        override = self.store.override(item_id)
        added = entry.get("added_at", now)
        return {
            "id": item_id,
            "source": "url",
            "url": entry["url"],
            "path": None,
            "filename": Path(urlparse(entry["url"]).path).name or entry["url"],
            "title": override.get("custom_title") or entry.get("title") or entry["url"],
            "original_title": entry.get("title"),
            "title_id": override.get("title_id"),
            "content_id": None,
            "version": None,
            "category": override.get("category") or "Link",
            "platform": "unknown",
            "size_bytes": 0,
            "size_label": "unknown",
            "mtime": added,
            "first_seen": added,
            "favorite": bool(override.get("favorite")),
            "hidden": bool(override.get("hidden")),
            "tags": override.get("tags") or [],
            "cover": None,
            "metadata_source": ["url"],
            "warnings": [],
        }

    def update_override(self, item_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Persist an override and refresh that item in the live index at once.

        Without this, a pin or rename would not be visible until the next
        background scan -- up to ``library.scan_interval_seconds`` later. Asking the
        scanner to re-run is still useful for picking up file changes, but the user's
        own edit has to land immediately.
        """
        self.store.set_override(item_id, **fields)

        with self._lock:
            existing = self._items.get(item_id)
        if existing is None:
            return None

        if existing.get("source") == "url":
            entry = next((e for e in self.store.urls() if e["id"] == item_id), None)
            refreshed = self._compose_url(entry, time.time()) if entry else None
        else:
            meta = self._metadata(Path(str(existing["path"])))
            refreshed = self._compose_local(item_id, meta, float(existing.get("first_seen") or 0.0))

        with self._lock:
            if refreshed is None:
                self._items.pop(item_id, None)
            else:
                self._items[item_id] = refreshed
        return refreshed

    def forget(self, item_id: str) -> None:
        """Drop an item from the live index (used when a URL entry is removed)."""
        with self._lock:
            self._items.pop(item_id, None)

    def add_url(self, url: str, title: Optional[str] = None) -> Dict[str, Any]:
        """Store a pasted link and put it in the live index straight away."""
        entry = self.store.add_url(url, title)
        item = self._compose_url(entry, time.time())
        with self._lock:
            self._items[entry["id"]] = item
        return item

    def _compose_local(self, item_id: str, meta: Dict[str, Any], first_seen: float) -> Dict[str, Any]:
        override = self.store.override(item_id)
        title_id = override.get("title_id") or meta.get("title_id")
        return {
            "id": item_id,
            "source": "local",
            "url": None,
            "path": meta["path"],
            "filename": meta["filename"],
            "title": override.get("custom_title") or meta["title"],
            "original_title": meta["title"],
            "title_id": title_id,
            "content_id": meta.get("content_id"),
            "version": meta.get("version"),
            "category": override.get("category") or meta.get("category") or "Unknown",
            "platform": meta.get("platform", "unknown"),
            "size_bytes": int(meta.get("size_bytes") or 0),
            "size_label": pkg_parser.format_size(meta.get("size_bytes") or 0),
            "mtime": meta.get("mtime") or 0.0,
            "first_seen": first_seen,
            "favorite": bool(override.get("favorite")),
            "hidden": bool(override.get("hidden")),
            "tags": override.get("tags") or [],
            "cover": self.cover_for(title_id),
            "metadata_source": meta.get("metadata_source") or [],
            "warnings": meta.get("warnings") or [],
        }

    # -- cover art ---------------------------------------------------------- #

    @staticmethod
    def cover_for(title_id: Optional[str]) -> Optional[str]:
        """Relative URL of a local cover image, or ``None`` for a placeholder.

        Only ``covers/<TITLEID>.<ext>`` on this machine is consulted. The UI draws
        a generated placeholder when this returns ``None``; nothing is fetched.
        """
        if not title_id:
            return None
        safe = re.sub(r"[^A-Za-z0-9_\-]", "", str(title_id))
        if not safe:
            return None
        for extension in (".png", ".jpg", ".jpeg", ".webp"):
            if (config.COVERS_DIR / f"{safe}{extension}").is_file():
                return f"/covers/{safe}{extension}"
        return None

    # -- queries ------------------------------------------------------------ #

    def all_items(self, include_hidden: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            items = [dict(item) for item in self._items.values()]
        if not include_hidden:
            items = [item for item in items if not item.get("hidden")]
        return items

    def item(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._items.get(item_id)
            return dict(found) if found else None

    def query(
        self,
        search: str = "",
        sort: str = "title",
        category: str = "",
        favorites_only: bool = False,
        include_hidden: bool = False,
    ) -> List[Dict[str, Any]]:
        """Filtered + sorted view. Favourites are always floated to the top."""
        items = self.all_items(include_hidden=include_hidden)

        needle = (search or "").strip().lower()
        if needle:
            terms = [term for term in needle.split() if term]

            def matches(item: Dict[str, Any]) -> bool:
                haystack = " ".join(
                    str(item.get(field) or "")
                    for field in ("title", "original_title", "filename", "title_id", "content_id", "category")
                ).lower()
                haystack += " " + " ".join(str(tag).lower() for tag in item.get("tags") or [])
                return all(term in haystack for term in terms)

            items = [item for item in items if matches(item)]

        if category:
            wanted = category.lower()
            items = [item for item in items if str(item.get("category", "")).lower() == wanted]

        if favorites_only:
            items = [item for item in items if item.get("favorite")]

        if sort == "size":
            items.sort(key=lambda item: (-int(item.get("size_bytes") or 0), str(item.get("title", "")).lower()))
        elif sort == "recent":
            items.sort(key=lambda item: (-float(item.get("first_seen") or 0.0),))
        elif sort == "category":
            items.sort(key=lambda item: (str(item.get("category", "")).lower(), str(item.get("title", "")).lower()))
        else:
            items.sort(key=lambda item: str(item.get("title", "")).lower())

        # Pinned titles lead the grid regardless of the active sort.
        items.sort(key=lambda item: 0 if item.get("favorite") else 1)
        return items

    def categories(self) -> List[str]:
        return sorted({str(item.get("category") or "Unknown") for item in self.all_items()})

    def status(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._items)
            total_bytes = sum(int(item.get("size_bytes") or 0) for item in self._items.values())
            return {
                "items": total,
                "total_bytes": total_bytes,
                "total_label": pkg_parser.format_size(total_bytes),
                "favorites": sum(1 for item in self._items.values() if item.get("favorite")),
                "last_scan": self._last_scan,
                "last_scan_ms": round(self._last_scan_duration * 1000),
                "scanning": self._scanning,
                "error": self._scan_error,
                "directories": [str(path) for path in config.pkg_directories()],
                "configured_directories": config.get("library.pkg_directories", []),
            }
