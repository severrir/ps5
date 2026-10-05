"""Browser-module endpoints: local history and bookmarks.

Note what is *not* here: no server-side fetch or proxy of the pages you visit.
Embedding happens entirely in the console's own iframe, so the hub never becomes a
relay for external traffic and the air-gap audit stays meaningful. Pages that refuse
to be framed are detected client-side and reported in the UI.

History lives in ``data/browser_history.json`` and is one of the paths the emergency
wipe targets by default.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from .. import config

bp = Blueprint("api_browser", __name__, url_prefix="/api/browser")

HISTORY_LIMIT = 300
_lock = threading.RLock()


def _history_path():
    return config.DATA_DIR / "browser_history.json"


def _load() -> List[Dict[str, Any]]:
    path = _history_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return [entry for entry in stored if isinstance(entry, dict)] if isinstance(stored, list) else []


def _save(entries: List[Dict[str, Any]]) -> None:
    try:
        config.write_atomic(_history_path(), json.dumps(entries[-HISTORY_LIMIT:], indent=1) + "\n")
    except OSError:
        pass


@bp.get("/history")
def get_history():
    with _lock:
        entries = _load()
    return jsonify({"ok": True, "history": entries[::-1], "limit": HISTORY_LIMIT})


@bp.post("/history")
def add_history():
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "no_url", "message": "No URL supplied."}), 400

    entry = {
        "url": url[:2000],
        "title": str(payload.get("title") or "")[:200] or url[:200],
        "at": time.time(),
        "framed": bool(payload.get("framed", True)),
    }
    with _lock:
        entries = _load()
        # Collapse an immediate repeat rather than filling history with refreshes.
        if entries and entries[-1].get("url") == entry["url"]:
            entries[-1] = entry
        else:
            entries.append(entry)
        _save(entries)
    return jsonify({"ok": True, "entry": entry})


@bp.delete("/history")
def clear_history():
    with _lock:
        path = _history_path()
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            return jsonify({"ok": False, "error": "delete_failed", "message": str(exc)}), 500
    return jsonify({"ok": True, "message": "Browser history cleared."})


@bp.get("/bookmarks")
def get_bookmarks():
    return jsonify(
        {
            "ok": True,
            "bookmarks": config.get("ui.browser_bookmarks", []) or [],
            "home": config.get("ui.browser_home", "about:blank"),
        }
    )


@bp.post("/bookmarks")
def set_bookmarks():
    """Replace the bookmark list. Each entry is ``{"title": ..., "url": ...}``."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    raw = payload.get("bookmarks")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "error": "bad_payload", "message": "Expected a bookmarks list."}), 400

    cleaned: List[Dict[str, str]] = []
    for entry in raw[:40]:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        cleaned.append({"title": str(entry.get("title") or url)[:120], "url": url[:2000]})

    config.save({"ui": {"browser_bookmarks": cleaned}})
    return jsonify({"ok": True, "bookmarks": cleaned, "message": f"Saved {len(cleaned)} bookmark(s)."})
