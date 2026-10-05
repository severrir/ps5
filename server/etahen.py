"""Client for the etaHEN daemons on the console, plus a connection watchdog.

Two daemons are addressed:

* **installer** (default port 12800) -- accepts a package install request and
  reports task progress.
* **control / klog** (default port 9090) -- heartbeat, kernel log, hardware.

Every request shape lives in ``configs.json -> etahen.endpoints``: method, path,
which port, and for ``install`` a JSON ``payload_template`` whose ``{url}`` and
``{title_id}`` tokens are substituted at call time. This matters because the
etaHEN wire format is not a stable public contract -- when a payload differs on
your firmware, it is a config edit, not a code change.

``etahen.mode`` selects the target: ``"live"`` talks to ``etahen.console_ip``,
``"mock"`` talks to :mod:`server.mock_etahen` on loopback. Nothing else in the
codebase branches on the mode, so the UI you test offline is the UI you run.

HTTP only, by configuration, using stdlib ``urllib`` -- no third-party HTTP
client is installed.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

from . import config
from .logs import logger


class EtaHenError(RuntimeError):
    """Raised when the console cannot be reached or answers unusably."""


def _substitute(value: Any, tokens: Dict[str, str]) -> Any:
    """Recursively replace ``{token}`` occurrences inside a payload template."""
    if isinstance(value, str):
        result = value
        for key, replacement in tokens.items():
            result = result.replace("{" + key + "}", replacement)
        return result
    if isinstance(value, list):
        return [_substitute(item, tokens) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, tokens) for key, item in value.items()}
    return value


class EtaHenClient:
    """Stateless-per-call HTTP client with a cached connection state summary."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {
            "connected": False,
            "last_ok": 0.0,
            "last_attempt": 0.0,
            "last_error": None,
            "consecutive_failures": 0,
            "handshakes": 0,
        }

    # -- targeting ---------------------------------------------------------- #

    @staticmethod
    def mode() -> str:
        return str(config.get("etahen.mode", "mock") or "mock").lower()

    @classmethod
    def target_ip(cls) -> str:
        if cls.mode() == "mock":
            return str(config.get("etahen.mock_bind_ip", "127.0.0.1") or "127.0.0.1")
        return str(config.get("etahen.console_ip", "") or "")

    @classmethod
    def resolve(cls, port_name: str) -> Tuple[str, int]:
        """``("192.168.1.50", 12800)`` for ``"installer"`` / ``"control"``."""
        key = "etahen.installer_port" if port_name == "installer" else "etahen.control_port"
        default = 12800 if port_name == "installer" else 9090
        return cls.target_ip(), int(config.get(key, default) or default)

    def describe_target(self) -> Dict[str, Any]:
        installer_ip, installer_port = self.resolve("installer")
        _control_ip, control_port = self.resolve("control")
        return {
            "mode": self.mode(),
            "ip": installer_ip,
            "installer_port": installer_port,
            "control_port": control_port,
        }

    # -- transport ---------------------------------------------------------- #

    def call(
        self,
        endpoint_name: str,
        tokens: Optional[Dict[str, str]] = None,
        payload_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Invoke a configured endpoint and return the decoded response.

        Raises :class:`EtaHenError` on transport failure. A non-JSON body is
        wrapped as ``{"raw": "<text>"}`` rather than treated as an error, because
        some etaHEN builds answer with plain text.
        """
        endpoints = config.get("etahen.endpoints", {}) or {}
        spec = endpoints.get(endpoint_name)
        if not isinstance(spec, dict) or not spec.get("path"):
            raise EtaHenError(f"No endpoint configured for '{endpoint_name}'")

        ip, port = self.resolve(str(spec.get("port", "installer")))
        if not ip:
            raise EtaHenError("etahen.console_ip is not set")

        method = str(spec.get("method", "GET")).upper()
        path = _substitute(str(spec["path"]), tokens or {})
        url = f"http://{ip}:{port}{path}"
        timeout = float(config.get("etahen.timeout_seconds", 8) or 8)

        body: Optional[bytes] = None
        headers = {"Accept": "application/json", "User-Agent": "PS5CommandHub/1.0"}
        if method in ("POST", "PUT", "PATCH"):
            payload = payload_override
            if payload is None:
                payload = _substitute(copy.deepcopy(spec.get("payload_template") or {}), tokens or {})
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urlrequest.Request(url, data=body, headers=headers, method=method)
        started = time.time()
        with self._lock:
            self._state["last_attempt"] = started

        try:
            with urlrequest.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = int(response.status or 0)
        except urlerror.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # pragma: no cover - best effort only
                detail = ""
            self._mark_failure(f"HTTP {exc.code} from {url}: {detail}")
            raise EtaHenError(f"HTTP {exc.code} from {endpoint_name}: {detail or exc.reason}") from exc
        except (urlerror.URLError, OSError, ValueError) as exc:
            self._mark_failure(f"{type(exc).__name__} contacting {url}: {exc}")
            raise EtaHenError(f"Could not reach {endpoint_name} at {ip}:{port}: {exc}") from exc

        self._mark_success()
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return {"ok": True, "status": status}
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": True, "status": status, "raw": text[:2000]}
        if isinstance(decoded, dict):
            decoded.setdefault("status", status)
            return decoded
        return {"ok": True, "status": status, "data": decoded}

    def _mark_success(self) -> None:
        with self._lock:
            was_down = not self._state["connected"]
            self._state["connected"] = True
            self._state["last_ok"] = time.time()
            self._state["last_error"] = None
            self._state["consecutive_failures"] = 0
            if was_down:
                self._state["handshakes"] = int(self._state["handshakes"]) + 1
                logger.info("etaHEN handshake established (%s)", self.describe_target()["ip"])

    def _mark_failure(self, message: str) -> None:
        with self._lock:
            self._state["connected"] = False
            self._state["last_error"] = message
            self._state["consecutive_failures"] = int(self._state["consecutive_failures"]) + 1
            failures = self._state["consecutive_failures"]
        # Quiet by design: a sleeping console should not spam the log.
        if failures == 1 or failures % 20 == 0:
            logger.warning("etaHEN unreachable (attempt %d): %s", failures, message)

    # -- operations --------------------------------------------------------- #

    def heartbeat(self) -> bool:
        try:
            self.call("heartbeat")
            return True
        except EtaHenError:
            return False

    def install(self, url: str, title_id: Optional[str] = None) -> Dict[str, Any]:
        """Ask the console to fetch and install ``url``.

        Returns the daemon's response. ``task_id`` is extracted when present so
        progress can be correlated; absence of one is not an error, it just means
        progress falls back to bytes-served accounting.
        """
        response = self.call("install", tokens={"url": url, "title_id": title_id or ""})
        task_id = (
            response.get("task_id")
            or response.get("id")
            or response.get("taskId")
            or response.get("task")
        )
        if task_id is not None:
            response["task_id"] = str(task_id)
        return response

    def progress(self) -> Dict[str, Any]:
        return self.call("progress")

    def storage(self) -> Dict[str, Any]:
        return self.call("storage")

    def hardware(self) -> Dict[str, Any]:
        return self.call("hardware")

    def klog(self, limit: int = 100) -> List[str]:
        response = self.call("klog")
        lines = response.get("lines") or response.get("log") or response.get("raw") or []
        if isinstance(lines, str):
            lines = lines.splitlines()
        return [str(line) for line in lines][-limit:]

    # -- state -------------------------------------------------------------- #

    def state(self) -> Dict[str, Any]:
        with self._lock:
            snapshot = dict(self._state)
        snapshot.update(self.describe_target())
        snapshot["age_seconds"] = (
            round(time.time() - snapshot["last_ok"], 1) if snapshot["last_ok"] else None
        )
        return snapshot


class Watchdog:
    """Re-establishes the etaHEN handshake on its own, with capped backoff.

    Deliberately silent: a console that is off or asleep is the normal case, not
    an error worth shouting about. The Safety and Library headers show the state;
    the log only records transitions.
    """

    def __init__(self, client: EtaHenClient) -> None:
        self.client = client
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._interval = float(config.get("etahen.watchdog_interval_seconds", 10) or 10)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="etahen-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        base = max(2.0, float(config.get("etahen.watchdog_interval_seconds", 10) or 10))
        ceiling = max(base, float(config.get("etahen.watchdog_backoff_max_seconds", 120) or 120))
        delay = base
        while not self._stop.is_set():
            ok = self.client.heartbeat()
            delay = base if ok else min(ceiling, delay * 2)
            self._stop.wait(delay)
