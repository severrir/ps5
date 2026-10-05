"""System endpoints: the aggregate poll, settings, logs, telemetry, backups.

``GET /api/system/state`` is the one endpoint the dashboard polls (once per second
by default). Everything the chrome needs -- console heartbeat, library counts, queue,
storage, hardware, safety pip -- is in that single response, which keeps the PS5
browser doing one request per tick instead of six.

Settings writes are restricted to an explicit allowlist of config paths. Notably
absent: everything under ``wipe``. Changing the confirmation phrase or PIN policy
goes through the PIN-gated endpoints in :mod:`server.routes.api_wipe`, so a
settings call can never weaken the destructive path.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from .. import config, fileserve, logs, pkg_parser, telemetry
from ..etahen import EtaHenError
from ..hub import hub
from ..logs import record_event

bp = Blueprint("api_system", __name__, url_prefix="/api/system")

# Config paths the UI may write. Anything not listed here is rejected, so adding a
# settings control is a deliberate act rather than an accident.
ALLOWED_SETTING_PATHS = {
    "ui.language",
    "ui.performance_mode",
    "ui.overscan_percent",
    "ui.cards_per_row",
    "ui.poll_interval_ms",
    "ui.toast_position",
    "ui.bgm_enabled",
    "ui.bgm_volume",
    "ui.sfx_enabled",
    "ui.rumble_enabled",
    "ui.browser_home",
    "ui.browser_bookmarks",
    "transfers.max_concurrent",
    "transfers.throttle_mbps",
    "transfers.max_retries",
    "transfers.retry_backoff_seconds",
    "library.pkg_directories",
    "library.scan_interval_seconds",
    "library.recursive",
    "etahen.mode",
    "etahen.console_ip",
    "etahen.installer_port",
    "etahen.control_port",
    "etahen.timeout_seconds",
    "safety.active_probing",
    "safety.log_retention_days",
    "safety.pihole.enabled",
    "safety.pihole.base_url",
    "safety.pihole.api_token",
    "backup.source_dirs",
}

AUDIO_EXTENSIONS = (".mp3", ".ogg", ".m4a", ".wav", ".flac", ".opus")


def _nest(dotted: str, value: Any) -> Dict[str, Any]:
    """``"ui.language", "en"`` -> ``{"ui": {"language": "en"}}``."""
    parts = dotted.split(".")
    node: Dict[str, Any] = {}
    cursor = node
    for part in parts[:-1]:
        cursor[part] = {}
        cursor = cursor[part]
    cursor[parts[-1]] = value
    return node


def _merge_into(target: Dict[str, Any], addition: Dict[str, Any]) -> None:
    for key, value in addition.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        else:
            target[key] = value


@bp.get("/state")
def state():
    """Everything the dashboard polls for, in one response."""
    return jsonify(hub().state())


@bp.get("/settings")
def get_settings():
    """Current settings plus the list of paths the UI is allowed to write."""
    current = config.raw()
    current.pop("wipe", None)  # never expose PIN material to the browser
    return jsonify(
        {
            "ok": True,
            "settings": current,
            "writable": sorted(ALLOWED_SETTING_PATHS),
            "resolved_host_ip": config.host_ip(),
            "base_url": config.base_url(),
            "app_root": str(config.APP_ROOT),
        }
    )


@bp.post("/settings")
def post_settings():
    """Write allowlisted settings. Body is ``{"<dotted.path>": value, ...}``."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"ok": False, "error": "no_changes", "message": "Nothing to save."}), 400

    rejected = [key for key in payload if key not in ALLOWED_SETTING_PATHS]
    if rejected:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "not_writable",
                    "message": "These settings are not writable from the UI: " + ", ".join(sorted(rejected)),
                    "rejected": sorted(rejected),
                }
            ),
            403,
        )

    updates: Dict[str, Any] = {}
    for dotted, value in payload.items():
        _merge_into(updates, _nest(dotted, value))

    config.save(updates)
    fileserve.apply_configured_throttle()
    hub().library.request_rescan()
    record_event("system.settings", "Settings updated: " + ", ".join(sorted(payload)))
    return jsonify({"ok": True, "message": "Settings saved.", "changed": sorted(payload)})


@bp.get("/logs")
def get_logs():
    """Ring-buffer tail for the developer log drawer."""
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    return jsonify({"ok": True, **logs.tail(after_seq=after)})


@bp.get("/events")
def get_events():
    """Structured session activity (queued, installed, failed, wipe, audit)."""
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200))))
    except ValueError:
        limit = 200
    return jsonify(
        {"ok": True, "events": logs.events(limit=limit, kind_prefix=request.args.get("kind") or None)[::-1]}
    )


@bp.get("/klog")
def get_klog():
    """Kernel log lines from the control daemon, when it exposes them."""
    try:
        lines = hub().client.klog(limit=150)
    except EtaHenError as exc:
        return jsonify({"ok": False, "error": "unreachable", "message": str(exc), "lines": []}), 200
    return jsonify({"ok": True, "lines": lines})


@bp.get("/storage")
def get_storage():
    return jsonify({"ok": True, **telemetry.storage(hub().client)})


@bp.get("/hardware")
def get_hardware():
    return jsonify({"ok": True, **telemetry.hardware(hub().client)})


@bp.get("/bgm")
def list_bgm():
    """Local background-audio files. Nothing is bundled; this lists your own."""
    tracks: List[Dict[str, Any]] = []
    if config.BGM_DIR.is_dir():
        for path in sorted(config.BGM_DIR.iterdir()):
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                tracks.append(
                    {
                        "name": path.stem,
                        "url": "/bgm/" + path.name,
                        "size_bytes": size,
                        "size_label": pkg_parser.format_size(size),
                    }
                )
    return jsonify(
        {
            "ok": True,
            "tracks": tracks,
            "folder": str(config.BGM_DIR),
            "hint": "Drop .mp3/.ogg/.m4a files into this folder; they are served locally only.",
        }
    )


@bp.get("/version")
def version_check():
    """Local-only update check.

    Looks for ``update/manifest.json`` beside the app (``{"version": "...",
    "notes": "..."}``). Staging that file is how you tell the hub an update is
    available -- there is no remote version service to contact, by design.
    """
    current = str(config.get("version", "1.0.0"))
    manifest_path = config.APP_ROOT / "update" / "manifest.json"
    staged: Dict[str, Any] | None = None
    if manifest_path.is_file():
        try:
            import json

            with open(manifest_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                staged = loaded
        except (OSError, ValueError):
            staged = None

    def parts(value: str) -> tuple:
        return tuple(int(piece) if piece.isdigit() else 0 for piece in str(value).split("."))

    newer = bool(staged and parts(str(staged.get("version", "0"))) > parts(current))
    return jsonify(
        {
            "ok": True,
            "version": current,
            "staged": staged,
            "update_available": newer,
            "manifest_path": str(manifest_path),
            "note": "Checked locally. The hub never contacts a remote update service.",
        }
    )


@bp.post("/backup")
def backup_saves():
    """Bundle the configured local folders into a timestamped zip.

    Honest about its limits: this backs up folders on *this PC*. Pulling save data
    off the console needs a console-side file API, which etaHEN does not expose by
    default -- configure ``backup.source_dirs`` with a folder you sync saves into.
    """
    sources = [Path(str(entry)) for entry in (config.get("backup.source_dirs", []) or [])]
    existing = [path for path in sources if path.is_dir()]
    if not existing:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "no_sources",
                    "message": (
                        "No backup sources configured. Set backup.source_dirs to the folder(s) "
                        "holding your saves (see README for getting saves off the console)."
                    ),
                    "configured": [str(path) for path in sources],
                }
            ),
            400,
        )

    output_dir = Path(str(config.get("backup.output_dir", str(config.APP_ROOT / "backups"))))
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = output_dir / f"savedata-{stamp}.zip"

    written = 0
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for root in existing:
                for path in root.rglob("*"):
                    if not path.is_file():
                        continue
                    bundle.write(path, Path(root.name) / path.relative_to(root))
                    written += 1
    except OSError as exc:
        return jsonify({"ok": False, "error": "backup_failed", "message": str(exc)}), 500

    keep = max(1, int(config.get("backup.keep_archives", 10) or 10))
    archives = sorted(output_dir.glob("savedata-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    pruned = 0
    for old in archives[keep:]:
        try:
            old.unlink()
            pruned += 1
        except OSError:
            continue

    size = archive.stat().st_size
    record_event("backup.created", f"Save backup created: {archive.name}", files=written)
    return jsonify(
        {
            "ok": True,
            "archive": str(archive),
            "files": written,
            "size_bytes": size,
            "size_label": pkg_parser.format_size(size),
            "pruned": pruned,
            "message": "Backed up {0} file(s) to {1} ({2}).".format(
                written, archive.name, pkg_parser.format_size(size)
            ),
        }
    )
