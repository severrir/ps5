"""The install queue: ordering, retries, pause/resume, and progress.

One worker thread owns all state transitions, so the API layer never has to reason
about races; it only enqueues intents. The queue is persisted to
``data/transfers.json`` after every change and reloaded at startup, so a hub
restart mid-queue resumes instead of forgetting.

**Progress** comes from the installer daemon when it exposes a usable progress
endpoint. When it does not, it is inferred from bytes this hub has actually served
for that package, and ``progress_source`` is set to ``"inferred"`` so the UI can
label it. Progress is never invented.

**Pause semantics, stated plainly.** A console-side install is not generally
pausable from off-console. What the hub can do, and does:

* a queued item simply never starts;
* a running item's byte stream is cut off -- the package route answers ``503``
  while paused, which aborts the console's fetch;
* on resume the install request is re-issued. Because the package route honours
  ``Range``, a console that re-requests with an offset continues rather than
  restarts.

If ``etahen.endpoints.task_control`` is configured (the mock daemon implements it),
the daemon is also asked to pause/resume its own task, and the result is reported.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from . import config, pkg_parser
from .etahen import EtaHenClient, EtaHenError
from .fileserve import apply_configured_throttle
from .library import Library
from .logs import logger, record_event

ACTIVE_STATES = ("starting", "installing")
TERMINAL_STATES = ("done", "failed", "cancelled")
WORKER_TICK_SECONDS = 0.5
PROGRESS_POLL_SECONDS = 1.0


def _new_id() -> str:
    return secrets.token_hex(5)


class TransferQueue:
    """Ordered install queue with a single owning worker thread."""

    def __init__(self, library: Library, client: EtaHenClient) -> None:
        self.library = library
        self.client = client
        self.path = config.DATA_DIR / "transfers.json"
        self._lock = threading.RLock()
        self._queue: List[Dict[str, Any]] = []
        self._history: Deque[Dict[str, Any]] = deque(
            maxlen=int(config.get("transfers.history_limit", 200) or 200)
        )
        self._paused_item_ids: set[str] = set()
        self._last_progress_poll = 0.0
        self._daemon_progress_supported: Optional[bool] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("transfers.json unreadable (%s); starting with an empty queue", exc)
            return
        if not isinstance(stored, dict):
            return

        for entry in stored.get("queue") or []:
            if not isinstance(entry, dict):
                continue
            # Anything that was mid-flight when the hub stopped goes back to
            # queued: the console is no longer streaming from a dead process.
            if entry.get("state") in ACTIVE_STATES:
                entry["state"] = "queued"
                entry["task_id"] = None
                entry["progress"] = 0.0
                entry["bytes_served"] = 0
                entry["note"] = "Requeued after hub restart"
            self._queue.append(entry)

        for entry in stored.get("history") or []:
            if isinstance(entry, dict):
                self._history.append(entry)

        if self._queue:
            logger.info("Restored %d queued transfer(s) from disk", len(self._queue))
        for entry in self._queue:
            if entry.get("state") == "paused":
                self._paused_item_ids.add(str(entry.get("item_id")))

    def _flush(self) -> None:
        payload = {"queue": self._queue, "history": list(self._history)}
        try:
            config.write_atomic(self.path, json.dumps(payload, indent=1) + "\n")
        except OSError as exc:
            logger.error("Could not persist transfers.json: %s", exc)

    def clear_all(self) -> None:
        """Drop queue, history and the on-disk copy (used by the wipe routine)."""
        with self._lock:
            self._queue = []
            self._history.clear()
            self._paused_item_ids.clear()
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        apply_configured_throttle()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="transfer-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - the worker must survive anything
                logger.exception("Transfer worker tick failed: %s", exc)
            self._wake.wait(WORKER_TICK_SECONDS)
            self._wake.clear()

    def _kick(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ #
    # Queue mutation
    # ------------------------------------------------------------------ #

    def enqueue(self, item_id: str) -> Dict[str, Any]:
        """Add a library item to the queue. Raises ``KeyError`` if unknown."""
        item = self.library.item(item_id)
        if item is None:
            raise KeyError(item_id)

        with self._lock:
            for entry in self._queue:
                if entry["item_id"] == item_id and entry["state"] not in TERMINAL_STATES:
                    return dict(entry)

            entry = {
                "id": _new_id(),
                "item_id": item_id,
                "title": item.get("title") or item.get("filename") or item_id,
                "source": item.get("source", "local"),
                "url": item.get("url"),
                "title_id": item.get("title_id"),
                "size_bytes": int(item.get("size_bytes") or 0),
                "size_label": item.get("size_label") or "unknown",
                "state": "queued",
                "progress": 0.0,
                "progress_source": "none",
                "task_id": None,
                "bytes_served": 0,
                "rate_bps": 0.0,
                "attempts": 0,
                "error": None,
                "note": None,
                "queued_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "_rate_mark": None,
            }
            self._queue.append(entry)
            self._flush()

        record_event("transfer.queued", f"Queued {entry['title']}", item_id=item_id, size=entry["size_bytes"])
        self._kick()
        return dict(entry)

    def enqueue_many(self, item_ids: List[str]) -> Dict[str, Any]:
        """Batch enqueue. Unknown ids are reported rather than aborting the batch."""
        added, missing = [], []
        for item_id in item_ids:
            try:
                added.append(self.enqueue(item_id))
            except KeyError:
                missing.append(item_id)
        return {"added": added, "missing": missing}

    def _find(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        for entry in self._queue:
            if entry["id"] == transfer_id:
                return entry
        return None

    def pause(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._find(transfer_id)
            if entry is None or entry["state"] in TERMINAL_STATES:
                return None
            entry["state"] = "paused"
            entry["rate_bps"] = 0.0
            entry["note"] = "Paused - byte stream cut; resume re-issues the install"
            self._paused_item_ids.add(str(entry["item_id"]))
            self._flush()
            snapshot = dict(entry)

        self._try_task_control(snapshot, "paused")
        record_event("transfer.paused", f"Paused {snapshot['title']}", transfer_id=transfer_id)
        return snapshot

    def resume(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._find(transfer_id)
            if entry is None or entry["state"] != "paused":
                return None
            self._paused_item_ids.discard(str(entry["item_id"]))
            # Re-enter the queue; the worker re-issues the install request and the
            # console continues via Range if it supports it.
            entry["state"] = "queued"
            entry["note"] = "Resumed"
            entry["task_id"] = None
            self._flush()
            snapshot = dict(entry)

        self._try_task_control(snapshot, "installing")
        record_event("transfer.resumed", f"Resumed {snapshot['title']}", transfer_id=transfer_id)
        self._kick()
        return snapshot

    def cancel(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._find(transfer_id)
            if entry is None:
                return None
            entry["state"] = "cancelled"
            entry["finished_at"] = time.time()
            entry["rate_bps"] = 0.0
            self._paused_item_ids.discard(str(entry["item_id"]))
            self._archive(entry)
            self._flush()
            snapshot = dict(entry)

        self._try_task_control(snapshot, "cancelled")
        record_event("transfer.cancelled", f"Cancelled {snapshot['title']}", transfer_id=transfer_id)
        return snapshot

    def retry(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._find(transfer_id)
            if entry is None:
                return None
            entry.update(
                {
                    "state": "queued",
                    "error": None,
                    "note": "Manual retry",
                    "attempts": 0,
                    "progress": 0.0,
                    "bytes_served": 0,
                    "task_id": None,
                    "finished_at": None,
                }
            )
            self._paused_item_ids.discard(str(entry["item_id"]))
            self._flush()
            snapshot = dict(entry)
        self._kick()
        return snapshot

    def remove(self, transfer_id: str) -> bool:
        """Remove an entry from the queue list. Does not touch any file."""
        with self._lock:
            before = len(self._queue)
            removed = [e for e in self._queue if e["id"] == transfer_id]
            self._queue = [e for e in self._queue if e["id"] != transfer_id]
            for entry in removed:
                self._paused_item_ids.discard(str(entry["item_id"]))
            changed = len(self._queue) != before
            if changed:
                self._flush()
            return changed

    def move(self, transfer_id: str, delta: int) -> bool:
        """Reorder within the queue. ``delta`` of -1 moves one place earlier."""
        with self._lock:
            index = next((i for i, e in enumerate(self._queue) if e["id"] == transfer_id), None)
            if index is None:
                return False
            target = max(0, min(len(self._queue) - 1, index + int(delta)))
            if target == index:
                return False
            entry = self._queue.pop(index)
            self._queue.insert(target, entry)
            self._flush()
            return True

    def clear_finished(self) -> int:
        with self._lock:
            before = len(self._queue)
            self._queue = [e for e in self._queue if e["state"] not in TERMINAL_STATES]
            removed = before - len(self._queue)
            if removed:
                self._flush()
            return removed

    # ------------------------------------------------------------------ #
    # Byte accounting (called from the package route)
    # ------------------------------------------------------------------ #

    def is_paused_item(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self._paused_item_ids

    def note_bytes(self, item_id: str, count: int) -> None:
        """Record bytes served for an item, for inferred progress and rate."""
        now = time.time()
        with self._lock:
            entry = next(
                (
                    e
                    for e in self._queue
                    if e["item_id"] == item_id and e["state"] in ACTIVE_STATES
                ),
                None,
            )
            if entry is None:
                return
            entry["bytes_served"] = int(entry.get("bytes_served") or 0) + int(count)
            mark = entry.get("_rate_mark")
            if mark is None:
                entry["_rate_mark"] = (now, entry["bytes_served"])
            else:
                last_time, last_bytes = mark
                span = now - last_time
                if span >= 0.5:
                    entry["rate_bps"] = (entry["bytes_served"] - last_bytes) / span
                    entry["_rate_mark"] = (now, entry["bytes_served"])

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #

    def tick(self) -> None:
        """One scheduling pass: start what can start, update what is running."""
        apply_configured_throttle()
        self._promote_due_retries()
        self._poll_progress()
        self._start_next()

    def _active_count(self) -> int:
        return sum(1 for e in self._queue if e["state"] in ACTIVE_STATES)

    def _start_next(self) -> None:
        limit = max(1, int(config.get("transfers.max_concurrent", 1) or 1))
        with self._lock:
            if self._active_count() >= limit:
                return
            candidate = next((e for e in self._queue if e["state"] == "queued"), None)
            if candidate is None:
                return
            candidate["state"] = "starting"
            candidate["started_at"] = candidate.get("started_at") or time.time()
            candidate["attempts"] = int(candidate.get("attempts") or 0) + 1
            candidate["error"] = None
            candidate["_rate_mark"] = None
            snapshot = dict(candidate)
            self._flush()

        url = self._package_url(snapshot)
        try:
            response = self.client.install(url, snapshot.get("title_id"))
        except EtaHenError as exc:
            self._fail(snapshot["id"], str(exc))
            return

        if response.get("ok") is False:
            self._fail(snapshot["id"], str(response.get("error") or "installer refused the request"))
            return

        with self._lock:
            entry = self._find(snapshot["id"])
            if entry is None or entry["state"] != "starting":
                return
            entry["state"] = "installing"
            entry["task_id"] = response.get("task_id")
            entry["note"] = None
            self._flush()

        record_event(
            "install.started",
            f"Install started: {snapshot['title']}",
            transfer_id=snapshot["id"],
            task_id=response.get("task_id"),
            url=url,
        )

    def _package_url(self, entry: Dict[str, Any]) -> str:
        """The URL handed to the console for this entry."""
        if entry.get("source") == "url" and entry.get("url"):
            return str(entry["url"])
        return f"{config.base_url()}/pkg/{entry['item_id']}"

    def _poll_progress(self) -> None:
        now = time.time()
        with self._lock:
            active = [dict(e) for e in self._queue if e["state"] in ACTIVE_STATES]
        if not active:
            return
        if now - self._last_progress_poll < PROGRESS_POLL_SECONDS:
            self._update_inferred(active)
            return
        self._last_progress_poll = now

        tasks_by_id: Dict[str, Dict[str, Any]] = {}
        try:
            response = self.client.progress()
            self._daemon_progress_supported = True
            tasks = response.get("tasks") or response.get("items") or []
            if isinstance(tasks, dict):
                tasks = list(tasks.values())
            for task in tasks:
                if isinstance(task, dict):
                    key = task.get("id") or task.get("task_id")
                    if key is not None:
                        tasks_by_id[str(key)] = task
        except EtaHenError:
            # No usable progress endpoint (or console gone). Inferred progress
            # takes over; the transfer itself is driven by the console pulling
            # bytes from us, so this is not a failure.
            if self._daemon_progress_supported is not False:
                self._daemon_progress_supported = False
                logger.info("Installer progress endpoint unavailable; using served-bytes estimate")

        for snapshot in active:
            task = tasks_by_id.get(str(snapshot.get("task_id"))) if snapshot.get("task_id") else None
            if task is not None:
                self._apply_daemon_task(snapshot["id"], task)
            else:
                self._apply_inferred(snapshot["id"])

    def _update_inferred(self, active: List[Dict[str, Any]]) -> None:
        for snapshot in active:
            if snapshot.get("progress_source") != "daemon":
                self._apply_inferred(snapshot["id"])

    def _apply_daemon_task(self, transfer_id: str, task: Dict[str, Any]) -> None:
        state = str(task.get("state") or task.get("status") or "installing").lower()
        with self._lock:
            entry = self._find(transfer_id)
            if entry is None or entry["state"] in TERMINAL_STATES:
                return
            total = int(task.get("total_bytes") or entry.get("size_bytes") or 0)
            installed = int(task.get("installed_bytes") or task.get("bytes") or 0)
            if task.get("progress") is not None:
                try:
                    entry["progress"] = max(0.0, min(100.0, float(task["progress"])))
                except (TypeError, ValueError):
                    pass
            elif total > 0:
                entry["progress"] = max(0.0, min(100.0, 100.0 * installed / total))
            entry["progress_source"] = "daemon"
            if task.get("rate_bps") is not None:
                try:
                    entry["rate_bps"] = float(task["rate_bps"])
                except (TypeError, ValueError):
                    pass
            if total and not entry.get("size_bytes"):
                entry["size_bytes"] = total
                entry["size_label"] = pkg_parser.format_size(total)
            finished = state in ("done", "finished", "complete", "completed", "installed")
            failed = state in ("failed", "error")
            self._flush()

        if finished:
            self._complete(transfer_id)
        elif failed:
            self._fail(transfer_id, str(task.get("error") or "installer reported failure"))

    def _apply_inferred(self, transfer_id: str) -> None:
        with self._lock:
            entry = self._find(transfer_id)
            if entry is None or entry["state"] not in ACTIVE_STATES:
                return
            size = int(entry.get("size_bytes") or 0)
            served = int(entry.get("bytes_served") or 0)
            if size > 0:
                entry["progress"] = max(0.0, min(100.0, 100.0 * served / size))
                entry["progress_source"] = "inferred"
                complete = served >= size
            else:
                entry["progress_source"] = "none"
                complete = False
            self._flush()
        if complete:
            # All bytes left this hub. For a URL item we never serve bytes, so
            # this only fires for locally hosted packages.
            self._complete(transfer_id, note="All bytes served; console-side install may still be finalising")

    def _complete(self, transfer_id: str, note: Optional[str] = None) -> None:
        with self._lock:
            entry = self._find(transfer_id)
            if entry is None or entry["state"] in TERMINAL_STATES:
                return
            entry["state"] = "done"
            entry["progress"] = 100.0
            entry["rate_bps"] = 0.0
            entry["finished_at"] = time.time()
            entry["note"] = note
            self._paused_item_ids.discard(str(entry["item_id"]))
            self._archive(entry)
            self._flush()
            title = entry["title"]
            duration = (entry["finished_at"] or 0) - (entry["started_at"] or entry["finished_at"] or 0)
        record_event(
            "install.done",
            f"Installed {title}",
            transfer_id=transfer_id,
            seconds=round(duration, 1),
            progress_source=entry.get("progress_source"),
        )
        self._kick()

    def _fail(self, transfer_id: str, message: str) -> None:
        backoff = max(1, int(config.get("transfers.retry_backoff_seconds", 5) or 5))
        max_retries = max(0, int(config.get("transfers.max_retries", 3) or 0))
        with self._lock:
            entry = self._find(transfer_id)
            if entry is None or entry["state"] in TERMINAL_STATES:
                return
            attempts = int(entry.get("attempts") or 0)
            entry["error"] = message
            entry["rate_bps"] = 0.0
            entry["task_id"] = None
            # max_retries counts attempts *after* the first, so the total number of
            # tries is max_retries + 1. attempts is already incremented by the time
            # we get here.
            if attempts - 1 < max_retries:
                delay = backoff * (2 ** (attempts - 1)) if attempts > 0 else backoff
                entry["state"] = "retrying"
                entry["retry_at"] = time.time() + delay
                entry["note"] = f"Attempt {attempts} failed; retrying in {int(delay)}s"
            else:
                entry["state"] = "failed"
                entry["finished_at"] = time.time()
                entry["note"] = f"Gave up after {attempts} attempt(s)"
                self._archive(entry)
            state = entry["state"]
            title = entry["title"]
            self._flush()

        if state == "failed":
            record_event("install.failed", f"Install failed: {title} - {message}", transfer_id=transfer_id)
        else:
            logger.warning("Transfer %s failed (%s); will retry", transfer_id, message)

    def _promote_due_retries(self) -> None:
        now = time.time()
        with self._lock:
            changed = False
            for entry in self._queue:
                if entry["state"] == "retrying" and float(entry.get("retry_at") or 0) <= now:
                    entry["state"] = "queued"
                    entry.pop("retry_at", None)
                    changed = True
            if changed:
                self._flush()

    def _archive(self, entry: Dict[str, Any]) -> None:
        """Copy a terminal entry into the bounded history list."""
        self._history.append(
            {
                "id": entry["id"],
                "item_id": entry["item_id"],
                "title": entry["title"],
                "state": entry["state"],
                "size_bytes": entry.get("size_bytes"),
                "size_label": entry.get("size_label"),
                "error": entry.get("error"),
                "note": entry.get("note"),
                "progress_source": entry.get("progress_source"),
                "queued_at": entry.get("queued_at"),
                "started_at": entry.get("started_at"),
                "finished_at": entry.get("finished_at"),
            }
        )

    def _try_task_control(self, entry: Dict[str, Any], state: str) -> Optional[Dict[str, Any]]:
        """Best-effort daemon-side pause/resume/cancel, when configured."""
        endpoints = config.get("etahen.endpoints", {}) or {}
        if "task_control" not in endpoints or not entry.get("task_id"):
            return None
        try:
            return self.client.call(
                "task_control",
                tokens={"task_id": str(entry["task_id"]), "state": state},
                payload_override={"task_id": str(entry["task_id"]), "state": state},
            )
        except EtaHenError as exc:
            logger.info("Daemon task_control(%s) not honoured: %s", state, exc)
            return None

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #

    @staticmethod
    def _public(entry: Dict[str, Any]) -> Dict[str, Any]:
        public = {key: value for key, value in entry.items() if not key.startswith("_")}
        public["rate_label"] = (
            f"{pkg_parser.format_size(int(entry.get('rate_bps') or 0))}/s"
            if (entry.get("rate_bps") or 0) > 0
            else ""
        )
        public["served_label"] = pkg_parser.format_size(int(entry.get("bytes_served") or 0))
        remaining = int(entry.get("size_bytes") or 0) - int(entry.get("bytes_served") or 0)
        rate = float(entry.get("rate_bps") or 0)
        public["eta_seconds"] = int(remaining / rate) if rate > 0 and remaining > 0 else None
        return public

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            queue = [self._public(entry) for entry in self._queue]
            history = list(self._history)[::-1]
            active = [e for e in queue if e["state"] in ACTIVE_STATES]
        return {
            "queue": queue,
            "history": history,
            "active": len(active),
            "pending": sum(1 for e in queue if e["state"] in ("queued", "retrying")),
            "paused": sum(1 for e in queue if e["state"] == "paused"),
            "max_concurrent": int(config.get("transfers.max_concurrent", 1) or 1),
            "throttle_mbps": float(config.get("transfers.throttle_mbps", 0) or 0),
            "throttle_presets_mbps": config.get("transfers.throttle_presets_mbps", []),
            "daemon_progress": self._daemon_progress_supported,
            "total_rate_bps": sum(float(e.get("rate_bps") or 0) for e in active),
        }

    def history(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)[-limit:][::-1]
