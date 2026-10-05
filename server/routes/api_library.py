"""Library endpoints: browsing, searching, renaming, pinning, and file removal.

Two destructive-ish operations are deliberately separated:

* ``DELETE /api/library/item/<id>`` forgets an item (hides a local package, drops a
  pasted URL). Nothing on disk changes.
* ``POST /api/library/item/<id>/delete-file`` deletes the actual ``.pkg``, requires
  ``{"confirm": true}``, and refuses any path that is not inside a configured
  package directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from flask import Blueprint, jsonify, request

from .. import config, pkg_parser
from ..hub import hub
from ..logs import record_event

bp = Blueprint("api_library", __name__, url_prefix="/api/library")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


@bp.get("")
def list_items():
    """Filtered, sorted library view plus scan status and category facets."""
    items = hub().library.query(
        search=request.args.get("search", ""),
        sort=request.args.get("sort", "title"),
        category=request.args.get("category", ""),
        favorites_only=_as_bool(request.args.get("favorites")),
        include_hidden=_as_bool(request.args.get("hidden")),
    )
    return jsonify(
        {
            "ok": True,
            "items": items,
            "count": len(items),
            "categories": hub().library.categories(),
            "status": hub().library.status(),
        }
    )


@bp.post("/rescan")
def rescan():
    """Scan the package directories immediately rather than waiting for the timer."""
    summary = hub().library.scan()
    return jsonify({"ok": True, **summary, "status": hub().library.status()})


@bp.get("/item/<item_id>")
def get_item(item_id: str):
    item = hub().library.item(item_id)
    if item is None:
        return jsonify({"ok": False, "error": "unknown_item", "message": "No such library item."}), 404
    return jsonify({"ok": True, "item": item})


@bp.post("/item/<item_id>/favorite")
def set_favorite(item_id: str):
    """Pin or unpin. Pinned titles lead the grid regardless of sort order."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    item = hub().library.item(item_id)
    if item is None:
        return jsonify({"ok": False, "error": "unknown_item", "message": "No such library item."}), 404
    favorite = _as_bool(payload.get("favorite"), not item.get("favorite"))
    hub().library.update_override(item_id, favorite=favorite or None)
    return jsonify({"ok": True, "favorite": favorite})


@bp.post("/item/<item_id>/rename")
def rename(item_id: str):
    """Set or clear a display title. The file on disk is never renamed."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    item = hub().library.item(item_id)
    if item is None:
        return jsonify({"ok": False, "error": "unknown_item", "message": "No such library item."}), 404

    title = str(payload.get("title") or "").strip()[:160]
    hub().library.update_override(item_id, custom_title=title or None)
    return jsonify(
        {
            "ok": True,
            "title": title or item.get("original_title"),
            "custom": bool(title),
        }
    )


@bp.post("/item/<item_id>/tags")
def set_tags(item_id: str):
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    raw = payload.get("tags") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    tags = [str(tag).strip()[:32] for tag in raw if str(tag).strip()][:12]
    hub().library.update_override(item_id, tags=tags or None)
    return jsonify({"ok": True, "tags": tags})


@bp.delete("/item/<item_id>")
def forget(item_id: str):
    """Remove an item from the library view without touching any file."""
    item = hub().library.item(item_id)
    if item is None:
        return jsonify({"ok": False, "error": "unknown_item", "message": "No such library item."}), 404

    if item.get("source") == "url":
        hub().library.store.remove_url(item_id)
        hub().library.forget(item_id)
        message = "Link removed from the library."
    else:
        hub().library.update_override(item_id, hidden=True)
        message = "Hidden from the library. The package file is untouched."
    return jsonify({"ok": True, "message": message})


@bp.post("/item/<item_id>/delete-file")
def delete_file(item_id: str):
    """Delete the package file itself. Explicit, confirmed, and path-checked."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    if not _as_bool(payload.get("confirm")):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "confirm_required",
                    "message": "Deleting a package file needs an explicit confirm.",
                }
            ),
            400,
        )

    item = hub().library.item(item_id)
    if item is None or not item.get("path"):
        return jsonify({"ok": False, "error": "unknown_item", "message": "No such local package."}), 404

    target = Path(str(item["path"])).resolve()
    roots = [Path(str(root)).resolve() for root in config.pkg_directories()]
    if not any(target == root or root in target.parents for root in roots):
        # Should be impossible via the UI; refuse loudly rather than trust the path.
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "outside_package_dirs",
                    "message": "That file is not inside a configured package directory.",
                }
            ),
            403,
        )

    size = int(item.get("size_bytes") or 0)
    try:
        target.unlink()
    except OSError as exc:
        return jsonify({"ok": False, "error": "delete_failed", "message": str(exc)}), 500

    hub().library.store.set_override(item_id, hidden=None, favorite=None, custom_title=None, tags=None)
    hub().library.forget(item_id)
    hub().library.request_rescan()
    record_event(
        "library.file_deleted",
        "Deleted package file: {0} ({1})".format(item.get("title"), pkg_parser.format_size(size)),
        path=str(target),
    )
    return jsonify(
        {
            "ok": True,
            "message": "Deleted {0}, freeing {1}.".format(target.name, pkg_parser.format_size(size)),
            "freed_bytes": size,
        }
    )


@bp.post("/url")
def add_url():
    """Add a direct package link. The console fetches it; the hub does not."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "bad_url",
                    "message": "Enter a direct http:// or https:// link to a package.",
                }
            ),
            400,
        )

    item = hub().library.add_url(url, str(payload.get("title") or "").strip() or None)
    record_event("library.url_added", f"Added link: {item['title']}", url=url)
    return jsonify({"ok": True, "item": item})


@bp.get("/categories")
def categories():
    return jsonify({"ok": True, "categories": hub().library.categories()})
