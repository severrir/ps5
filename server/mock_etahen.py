"""An offline stand-in for the etaHEN daemons.

Runs two small HTTP servers on loopback that speak the same shapes the hub
expects from the console: the installer on ``etahen.installer_port`` and the
control/klog socket on ``etahen.control_port``.

Why this exists: the real wire format can only be confirmed against a console,
and a console is not always on. With ``etahen.mode: "mock"`` the whole hub --
queue, progress, throttle, storage meter, telemetry, log drawer -- is exercisable
on the PC alone, and the test suite can assert real behaviour instead of mocking
out the integration boundary.

Install tasks advance on a simulated clock at ``MOCK_RATE_BYTES_PER_SEC``. Total
sizes are taken from a ``HEAD`` against the URL the hub supplied (which is the
hub's own file server), so progress tracks real package sizes.

Run standalone with::

    python -m server.mock_etahen
"""

from __future__ import annotations

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

from . import config

MOCK_RATE_BYTES_PER_SEC = 180 * 1024 * 1024  # ~180 MB/s, a plausible LAN install
DEFAULT_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MOCK_INTERNAL_TOTAL = 825 * 1000 * 1000 * 1000
MOCK_EXTERNAL_TOTAL = 2 * 1000 * 1000 * 1000 * 1000

KLOG_SAMPLES = (
    "[etaHEN] daemon alive",
    "[etaHEN] klog listener ready",
    "[pkg_installer] idle",
    "[sys] shellui heartbeat ok",
    "[etaHEN] elf loader ready",
)


class MockState:
    """Shared, thread-safe state for both mock daemons."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._counter = 0
        self._booted = time.time()
        self._klog: List[str] = [f"{line}" for line in KLOG_SAMPLES]
        self.installed_bytes_total = 0

    # -- tasks -------------------------------------------------------------- #

    def create_task(self, url: str) -> Dict[str, Any]:
        # Probe outside the lock: this is a blocking HTTP round trip, and holding the
        # state lock across it would stall every /api/progress request for its
        # duration -- which is exactly when the UI is polling hardest.
        total_bytes = self._probe_size(url)

        with self._lock:
            self._counter += 1
            task_id = f"mock-{self._counter:04d}"
            task = {
                "id": task_id,
                "url": url,
                "total_bytes": total_bytes,
                "started": time.time(),
                "state": "installing",
                "paused_for": 0.0,
                "paused_at": None,
                "downloaded": 0,
                # "pending" until the download thread reports back, then "real" or
                # "unavailable". Progress holds at zero while pending: falling back to
                # the simulated clock immediately would race the fetch and complete a
                # small package before a single real byte was credited.
                "fetch": "pending",
                "real_download": False,
                "sim_started": None,
            }
            self._tasks[task_id] = task
            self._klog.append(f"[pkg_installer] begin {task_id} {url}")

        # Pull the package for real, exactly as a console would. This is what makes
        # the mock an integration harness rather than a stub: it drives the hub's own
        # file server, its Range support and its bandwidth throttle.
        threading.Thread(
            target=self._download, args=(task_id, url), name=f"mock-download-{task_id}", daemon=True
        ).start()
        return self.snapshot_task(task_id) or {}

    @staticmethod
    def _probe_size(url: str) -> int:
        """HEAD the URL so progress has a denominator before bytes arrive."""
        try:
            request = urlrequest.Request(url, method="HEAD")
            with urlrequest.urlopen(request, timeout=3) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > 0:
                    return int(length)
        except (urlerror.URLError, OSError, ValueError):
            pass
        return DEFAULT_TOTAL_BYTES

    def _download(self, task_id: str, url: str) -> None:
        """Stream the package to /dev/null, counting bytes and honouring pauses."""
        try:
            with urlrequest.urlopen(url, timeout=10) as response:
                length = response.headers.get("Content-Length")
                with self._lock:
                    task = self._tasks.get(task_id)
                    if task:
                        # Mark the fetch real as soon as the response opens, before any
                        # body is read, so a tiny package cannot finish on the
                        # simulated clock in the gap.
                        task["fetch"] = "real"
                        task["real_download"] = True
                        if length and int(length) > 0:
                            task["total_bytes"] = int(length)

                while True:
                    with self._lock:
                        task = self._tasks.get(task_id)
                        if task is None or task["state"] in ("cancelled", "failed"):
                            return
                        paused = task["state"] == "paused"
                    if paused:
                        time.sleep(0.1)
                        continue

                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    with self._lock:
                        task = self._tasks.get(task_id)
                        if task is None:
                            return
                        task["downloaded"] = int(task["downloaded"]) + len(chunk)
                        self.installed_bytes_total += len(chunk)

            with self._lock:
                task = self._tasks.get(task_id)
                if task and task["state"] == "installing":
                    task["state"] = "done"
                    task["downloaded"] = task["total_bytes"]
                    self._klog.append(f"[pkg_installer] finished {task_id}")
        except (urlerror.URLError, OSError, ValueError) as exc:
            # A URL the mock cannot fetch (a remote link, or a paused 503) falls back
            # to the simulated clock rather than failing the task outright.
            with self._lock:
                task = self._tasks.get(task_id)
                if task and task["fetch"] == "pending":
                    task["fetch"] = "unavailable"
                    task["sim_started"] = time.time()
                    self._klog.append(f"[pkg_installer] {task_id} fetch unavailable ({exc}); simulating")

    def _advance(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Update the task's byte position.

        Real downloads report what actually arrived. Anything the mock could not
        fetch falls back to a simulated clock, so the UI still has something to show.
        """
        total = int(task["total_bytes"])
        fetch = task.get("fetch", "pending")

        if fetch == "real":
            installed = min(total, int(task.get("downloaded") or 0))
        elif fetch == "pending":
            # The fetch has not reported back yet. Report no progress rather than
            # guessing; the next poll will have an answer either way.
            installed = min(total, int(task.get("downloaded") or 0))
        else:
            origin = float(task.get("sim_started") or task["started"])
            if task["state"] in ("done", "failed", "cancelled"):
                elapsed = 0.0
            elif task["state"] == "paused":
                elapsed = float(task["paused_at"] or origin) - origin - task["paused_for"]
            else:
                elapsed = time.time() - origin - task["paused_for"]
            installed = min(total, int(max(0.0, elapsed) * MOCK_RATE_BYTES_PER_SEC))

        if installed >= total and task["state"] == "installing":
            task["state"] = "done"
            self._klog.append(f"[pkg_installer] finished {task['id']}")
        task["installed_bytes"] = installed
        return task

    def snapshot_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            self._advance(task)
            total = max(1, int(task["total_bytes"]))
            return {
                "id": task["id"],
                "url": task["url"],
                "state": task["state"],
                "installed_bytes": int(task["installed_bytes"]),
                "total_bytes": int(task["total_bytes"]),
                "progress": round(100.0 * task["installed_bytes"] / total, 2),
                "rate_bps": self._rate_for(task),
                "real_download": bool(task.get("real_download")),
            }

    @staticmethod
    def _rate_for(task: Dict[str, Any]) -> int:
        """Observed rate for a real download, the simulated one otherwise."""
        if task["state"] != "installing":
            return 0
        fetch = task.get("fetch", "pending")
        if fetch == "pending":
            return 0
        if fetch != "real":
            return MOCK_RATE_BYTES_PER_SEC
        elapsed = max(0.001, time.time() - float(task["started"]) - float(task["paused_for"]))
        return int(int(task.get("downloaded") or 0) / elapsed)

    def snapshot_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(self._tasks.keys())
        return [snap for snap in (self.snapshot_task(tid) for tid in ids) if snap]

    def set_state(self, task_id: str, state: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            self._advance(task)
            if state == "paused" and task["state"] == "installing":
                task["paused_at"] = time.time()
                task["state"] = "paused"
            elif state == "installing" and task["state"] == "paused":
                task["paused_for"] += time.time() - float(task["paused_at"] or time.time())
                task["paused_at"] = None
                task["state"] = "installing"
            else:
                task["state"] = state
            self._klog.append(f"[pkg_installer] {task_id} -> {task['state']}")
            return True

    # -- other surfaces ----------------------------------------------------- #

    def storage(self) -> Dict[str, Any]:
        used_internal = sum(
            int(task.get("total_bytes", 0)) for task in self.snapshot_all() if task["state"] == "done"
        )
        return {
            "ok": True,
            "internal": {
                "total_bytes": MOCK_INTERNAL_TOTAL,
                "free_bytes": max(0, MOCK_INTERNAL_TOTAL - 310_000_000_000 - used_internal),
                "label": "Internal SSD",
            },
            "external": {
                "total_bytes": MOCK_EXTERNAL_TOTAL,
                "free_bytes": MOCK_EXTERNAL_TOTAL - 640_000_000_000,
                "label": "Extended Storage",
            },
        }

    def hardware(self) -> Dict[str, Any]:
        # Plausible, clearly synthetic values; the UI labels them as mock.
        jitter = random.uniform(-1.5, 1.5)
        return {
            "ok": True,
            "soc_temp_c": round(52.0 + jitter, 1),
            "fan_rpm": int(1450 + jitter * 40),
            "cpu_load_percent": round(max(2.0, 14.0 + jitter * 3), 1),
            "uptime_seconds": int(time.time() - self._booted),
            "simulated": True,
        }

    def heartbeat(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "etahen_version": "2.x-mock",
            "uptime_seconds": int(time.time() - self._booted),
            "simulated": True,
        }

    def klog(self, limit: int = 120) -> Dict[str, Any]:
        with self._lock:
            lines = self._klog[-limit:]
        return {"ok": True, "lines": lines, "simulated": True}


STATE = MockState()


def reset_state() -> MockState:
    """Discard all simulated tasks and log lines.

    The handlers resolve ``STATE`` from module globals on each request, so swapping
    it here is enough. Used between tests, and useful from a REPL when a long mock
    session has accumulated junk.
    """
    global STATE
    STATE = MockState()
    return STATE


class _Handler(BaseHTTPRequestHandler):
    """Shared request handling for both mock ports."""

    server_version = "MockEtaHEN/1.0"
    role = "installer"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Silence the default stderr spam; the hub's own logger is the record.
        return

    def _send(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, OSError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/heartbeat":
            return self._send(STATE.heartbeat())
        if path == "/api/progress":
            return self._send({"ok": True, "tasks": STATE.snapshot_all(), "simulated": True})
        if path == "/api/storage":
            return self._send(STATE.storage())
        if path == "/api/hardware":
            return self._send(STATE.hardware())
        if path == "/api/klog":
            return self._send(STATE.klog())
        return self._send({"ok": False, "error": "not_found", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        payload = self._read_json()
        if path == "/api/install":
            packages = payload.get("packages") or payload.get("urls") or []
            if isinstance(packages, str):
                packages = [packages]
            if not packages:
                return self._send({"ok": False, "error": "no_packages"}, 400)
            created = [STATE.create_task(str(url)) for url in packages]
            first = created[0] if created else {}
            return self._send({"ok": True, "task_id": first.get("id"), "tasks": created})
        if path == "/api/task":
            task_id = str(payload.get("task_id") or "")
            state = str(payload.get("state") or "")
            if STATE.set_state(task_id, state):
                return self._send({"ok": True, "task": STATE.snapshot_task(task_id)})
            return self._send({"ok": False, "error": "unknown_task"}, 404)
        return self._send({"ok": False, "error": "not_found", "path": path}, 404)


def _serve(bind_ip: str, port: int, role: str) -> Tuple[ThreadingHTTPServer, threading.Thread]:
    handler = type(f"_Handler_{role}", (_Handler,), {"role": role})
    httpd = ThreadingHTTPServer((bind_ip, port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name=f"mock-etahen-{role}", daemon=True)
    thread.start()
    return httpd, thread


def start_in_process() -> List[ThreadingHTTPServer]:
    """Start both mock daemons in this process. Returns the server objects.

    Binding failures are swallowed deliberately: if a standalone mock (or the
    real console tunnel) already holds the port, the hub should still boot.
    """
    bind_ip = str(config.get("etahen.mock_bind_ip", "127.0.0.1") or "127.0.0.1")
    installer_port = int(config.get("etahen.installer_port", 12800) or 12800)
    control_port = int(config.get("etahen.control_port", 9090) or 9090)

    servers: List[ThreadingHTTPServer] = []
    for port, role in ((installer_port, "installer"), (control_port, "control")):
        try:
            httpd, _thread = _serve(bind_ip, port, role)
            servers.append(httpd)
        except OSError as exc:
            print(f"[mock-etahen] could not bind {bind_ip}:{port} ({exc}) - assuming one is already running")
    return servers


def main() -> None:
    config.load()
    servers = start_in_process()
    if not servers:
        print("[mock-etahen] nothing bound; exiting")
        return
    bound = ", ".join(f"{s.server_address[0]}:{s.server_address[1]}" for s in servers)
    print(f"[mock-etahen] listening on {bound} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[mock-etahen] stopping")
        for server in servers:
            server.shutdown()


if __name__ == "__main__":
    main()
