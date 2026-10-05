"""Range-capable package serving with a real bandwidth throttle.

The console pulls packages from this hub, which is what makes the throttle
meaningful: we own the socket, so a token bucket on our side genuinely caps the
transfer rate instead of merely suggesting it. (A transfer the console initiates
from somewhere else cannot be shaped from here, and the UI says so.)

``Range`` is honoured so an interrupted install can resume rather than restart,
and every chunk written is reported back through an ``on_bytes`` callback -- that
accounting is what powers the inferred-progress fallback in
:mod:`server.transfers` when the installer daemon exposes no progress endpoint.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from flask import Response, request

from . import config
from .logs import logger

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)
MIB = 1024 * 1024


class TokenBucket:
    """Shared byte budget. ``rate <= 0`` means unlimited.

    Implemented as a virtual-finish-time shaper rather than a refilling token
    counter: each caller reserves ``amount / rate`` seconds of the shared timeline
    and sleeps until its slot arrives. That keeps the long-run average exactly on
    rate even when a single chunk is larger than the burst allowance -- a counter
    clamped to a burst capacity silently throttles below the configured rate in
    that case, which is wrong in precisely the situation (a low limit with a big
    chunk size) where the limit matters most.

    Blocking is fine here: the WSGI server is threaded, so sleeping inside a
    response generator slows that one transfer, not the UI. The lock is never held
    across the sleep.
    """

    def __init__(self, rate_bytes_per_sec: float = 0.0, burst_seconds: float = 0.5) -> None:
        self._lock = threading.Lock()
        self._rate = max(0.0, float(rate_bytes_per_sec))
        self._burst_seconds = max(0.05, burst_seconds)
        self._next = time.monotonic()

    @property
    def rate(self) -> float:
        with self._lock:
            return self._rate

    def set_rate(self, rate_bytes_per_sec: float) -> None:
        with self._lock:
            self._rate = max(0.0, float(rate_bytes_per_sec))
            # Start a fresh timeline so a raised limit takes effect immediately
            # instead of working through slots reserved at the old rate.
            self._next = time.monotonic()

    def consume(self, amount: int) -> float:
        """Block until ``amount`` bytes may be sent. Returns seconds waited."""
        if amount <= 0:
            return 0.0

        with self._lock:
            if self._rate <= 0:
                return 0.0
            now = time.monotonic()
            # Idle time earns credit, but only up to the burst window -- otherwise a
            # transfer that starts after a long pause would run unthrottled.
            earliest = now - self._burst_seconds
            if self._next < earliest:
                self._next = earliest
            wait = self._next - now
            self._next += amount / self._rate

        if wait > 0:
            time.sleep(wait)
            return wait
        return 0.0


_bucket = TokenBucket()


def bucket() -> TokenBucket:
    return _bucket


def apply_configured_throttle() -> float:
    """Sync the bucket with ``transfers.throttle_mbps``. Returns bytes/sec."""
    mbps = float(config.get("transfers.throttle_mbps", 0) or 0)
    rate = max(0.0, mbps) * MIB
    _bucket.set_rate(rate)
    return rate


def set_throttle_mbps(mbps: float) -> float:
    """Persist a new throttle and apply it immediately."""
    value = max(0.0, float(mbps))
    config.save({"transfers": {"throttle_mbps": value}})
    rate = apply_configured_throttle()
    logger.info("Throttle set to %s", "unlimited" if value <= 0 else f"{value:g} MB/s")
    return rate


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """Parse a single-range ``Range`` header into inclusive ``(start, end)``.

    Returns ``None`` for an absent or unsatisfiable header; multi-range requests
    are intentionally not supported (no installer needs them) and fall back to a
    full-body response.
    """
    if not header or size <= 0:
        return None
    match = RANGE_RE.match(header.strip())
    if not match:
        return None
    raw_start, raw_end = match.group(1), match.group(2)
    if raw_start == "" and raw_end == "":
        return None
    if raw_start == "":
        length = int(raw_end)
        if length <= 0:
            return None
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


def send_package_file(
    path: Path,
    download_name: Optional[str] = None,
    on_bytes: Optional[Callable[[int], None]] = None,
) -> Response:
    """Stream ``path`` with Range support, throttling, and byte accounting."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.error("Cannot stat %s: %s", path, exc)
        return Response("package unavailable", status=404, mimetype="text/plain")

    chunk_size = max(16 * 1024, int(config.get("transfers.chunk_bytes", 262144) or 262144))
    requested = parse_range(request.headers.get("Range"), size)

    if requested is None and request.headers.get("Range"):
        # Present but unsatisfiable -- answer honestly rather than silently
        # sending the whole file.
        response = Response(status=416, mimetype="text/plain")
        response.headers["Content-Range"] = f"bytes */{size}"
        response.headers["Accept-Ranges"] = "bytes"
        return response

    start, end = requested if requested else (0, max(0, size - 1))
    length = 0 if size == 0 else end - start + 1

    def generate():
        sent = 0
        try:
            with open(path, "rb", buffering=0) as handle:
                handle.seek(start)
                while sent < length:
                    want = min(chunk_size, length - sent)
                    _bucket.consume(want)
                    data = handle.read(want)
                    if not data:
                        break
                    sent += len(data)
                    if on_bytes is not None:
                        try:
                            on_bytes(len(data))
                        except Exception:  # pragma: no cover - accounting must not break a transfer
                            pass
                    yield data
        except GeneratorExit:
            # The console closed the connection (pause, cancel, sleep). Normal.
            logger.info("Transfer of %s interrupted after %d byte(s)", path.name, sent)
            raise
        except OSError as exc:
            logger.error("Read error streaming %s: %s", path, exc)

    response = Response(generate(), mimetype="application/octet-stream", direct_passthrough=True)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{os.path.basename(download_name or path.name)}"'
    )
    if requested is not None:
        response.status_code = 206
        response.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return response
