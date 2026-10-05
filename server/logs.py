"""Logging, the live log drawer buffer, and the session activity log.

Three consumers, one pipeline:

* ``logs/session-YYYYMMDD.log`` -- plain text, rotated by date, pruned after
  ``safety.log_retention_days``.
* an in-memory ring buffer -- what the hidden developer drawer in the UI streams
  via ``GET /api/system/logs``. Bounded, so it cannot grow without limit.
* ``data/session_events.json`` -- structured events (queued, installed, failed,
  wipe attempted) for the Activity view and for auditing after the fact.

Everything is local. Nothing is transmitted.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List

from . import config

RING_CAPACITY = 800
EVENT_CAPACITY = 500

_ring: Deque[Dict[str, Any]] = deque(maxlen=RING_CAPACITY)
_events: Deque[Dict[str, Any]] = deque(maxlen=EVENT_CAPACITY)
_seq = 0
_lock = threading.RLock()
_configured = False

logger = logging.getLogger("ps5hub")


class _RingHandler(logging.Handler):
    """Mirrors every log record into the in-memory buffer for the UI drawer."""

    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - formatting must never kill logging
            message = record.getMessage()
        with _lock:
            _seq += 1
            _ring.append(
                {
                    "seq": _seq,
                    "ts": record.created,
                    "time": _dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                    "level": record.levelname,
                    "name": record.name,
                    "message": message,
                }
            )


def _events_path() -> Path:
    return config.DATA_DIR / "session_events.json"


def _load_events() -> None:
    """Restore structured events from the previous run, if any."""
    path = _events_path()
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(stored, list):
        for item in stored[-EVENT_CAPACITY:]:
            if isinstance(item, dict):
                _events.append(item)


def _persist_events() -> None:
    try:
        config.write_atomic(_events_path(), json.dumps(list(_events), indent=1) + "\n")
    except OSError:
        pass


def prune_old_logs() -> int:
    """Delete session logs older than the retention window. Returns count removed."""
    days = int(config.get("safety.log_retention_days", 7) or 7)
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in config.LOG_DIR.glob("session-*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def setup(level: int = logging.INFO) -> logging.Logger:
    """Attach file + console + ring handlers exactly once."""
    global _configured
    with _lock:
        if _configured:
            return logger

        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        today = _dt.date.today().strftime("%Y%m%d")
        log_path = config.LOG_DIR / f"session-{today}.log"

        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s", "%H:%M:%S")

        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(fmt)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)

        ring_handler = _RingHandler()
        ring_handler.setFormatter(logging.Formatter("%(message)s"))

        logger.setLevel(level)
        logger.handlers = [file_handler, stream_handler, ring_handler]
        logger.propagate = False

        _load_events()
        removed = prune_old_logs()
        if removed:
            logger.info("Pruned %d expired session log(s)", removed)

        _configured = True
        return logger


def tail(after_seq: int = 0, limit: int = 300) -> Dict[str, Any]:
    """Return ring-buffer lines newer than ``after_seq`` for the log drawer."""
    with _lock:
        lines = [entry for entry in _ring if entry["seq"] > after_seq]
    if len(lines) > limit:
        lines = lines[-limit:]
    return {"lines": lines, "last_seq": _seq, "capacity": RING_CAPACITY}


def record_event(kind: str, message: str, **detail: Any) -> Dict[str, Any]:
    """Append a structured activity event and log it.

    ``kind`` is a short slug used by the UI for icons/filtering, e.g.
    ``transfer.queued``, ``install.done``, ``wipe.attempt``, ``safety.audit``.
    """
    event = {
        "ts": time.time(),
        "iso": _dt.datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "message": message,
        "detail": detail or {},
    }
    with _lock:
        _events.append(event)
        _persist_events()
    logger.info("[%s] %s", kind, message)
    return event


def events(limit: int = 200, kind_prefix: str | None = None) -> List[Dict[str, Any]]:
    """Most recent activity events, newest last."""
    with _lock:
        items = list(_events)
    if kind_prefix:
        items = [item for item in items if str(item.get("kind", "")).startswith(kind_prefix)]
    return items[-limit:]


def clear_events() -> None:
    """Drop structured events and their on-disk copy (used by the wipe routine)."""
    with _lock:
        _events.clear()
        path = _events_path()
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
