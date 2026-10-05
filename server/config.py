"""Configuration store for the PS5 Command Hub.

Everything tunable lives in ``configs.json`` beside ``run.py``. This module owns:

* ``DEFAULTS`` -- the authoritative runtime schema. ``configs.example.json`` is a
  documented copy of it; ``tests/test_config.py`` asserts the two never drift.
* First-run bootstrap -- if ``configs.json`` is missing it is created from the
  defaults so the hub always starts with something valid.
* Token expansion -- any string value may contain ``{app}``, which expands to the
  folder containing ``run.py``. That keeps the shipped config machine-agnostic.
* Atomic saves -- settings changed from the UI are written via a temp file and
  ``os.replace`` so a crash mid-write can never truncate your config.

No value in here is ever sent anywhere. The hub makes no outbound requests except
the ones you explicitly enable (``safety.active_probing``, ``safety.pihole``) and
the LAN calls to your own console.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Dict, Iterator

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_ROOT / "configs.json"
EXAMPLE_PATH = APP_ROOT / "configs.example.json"
DATA_DIR = APP_ROOT / "data"
LOG_DIR = APP_ROOT / "logs"
COVERS_DIR = APP_ROOT / "covers"
BGM_DIR = APP_ROOT / "bgm"

# Paths the wipe routine must never be able to touch. Defined once, here, so the
# allowlist check and the tests share a single source of truth.
PROTECTED_PATHS = (
    APP_ROOT / "server",
    APP_ROOT / "static",
    APP_ROOT / "templates",
    APP_ROOT / "tests",
    APP_ROOT / "pkg_wrapper",
    APP_ROOT / "run.py",
    APP_ROOT / "requirements.txt",
    APP_ROOT / "configs.example.json",
    APP_ROOT / "configs.json",
)

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

DEFAULTS: Dict[str, Any] = {
    "version": "1.0.0",
    "server": {
        # "auto" resolves the primary LAN address of this PC at startup.
        "host_ip": "auto",
        "port": 8080,
        "subnet_allowlist": [
            "192.168.0.0/16",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "127.0.0.0/8",
        ],
        "enforce_subnet_allowlist": True,
    },
    "etahen": {
        # "mock" talks to server/mock_etahen.py on loopback; "live" talks to the
        # console at etahen.console_ip. Nothing else in the code branches on this.
        "mode": "mock",
        "console_ip": "192.168.1.50",
        "installer_port": 12800,
        "control_port": 9090,
        "timeout_seconds": 8,
        "watchdog_interval_seconds": 10,
        "watchdog_backoff_max_seconds": 120,
        "mock_bind_ip": "127.0.0.1",
        # Every request shape the hub sends lives here, so the wire format can be
        # corrected against a real console without editing Python. "{url}",
        # "{title_id}" and "{task_id}" are substituted at call time.
        "endpoints": {
            "install": {
                "port": "installer",
                "method": "POST",
                "path": "/api/install",
                "payload_template": {"type": "direct", "packages": ["{url}"]},
            },
            "progress": {"port": "installer", "method": "GET", "path": "/api/progress"},
            # Optional. Remove this key if your firmware's daemon has no task
            # control; pause/resume then works purely by cutting the byte stream.
            "task_control": {
                "port": "installer",
                "method": "POST",
                "path": "/api/task",
                "payload_template": {"task_id": "{task_id}", "state": "{state}"},
            },
            "storage": {"port": "installer", "method": "GET", "path": "/api/storage"},
            "heartbeat": {"port": "control", "method": "GET", "path": "/api/heartbeat"},
            "klog": {"port": "control", "method": "GET", "path": "/api/klog"},
            "hardware": {"port": "control", "method": "GET", "path": "/api/hardware"},
        },
    },
    "library": {
        "pkg_directories": ["{app}/packages"],
        "scan_interval_seconds": 60,
        "recursive": True,
        "cache_ttl_seconds": 300,
        "max_items": 5000,
        "extensions": [".pkg"],
    },
    "transfers": {
        "max_concurrent": 1,
        # Retries *after* the first attempt, so 3 means up to 4 tries in total.
        "max_retries": 3,
        "retry_backoff_seconds": 5,
        # 0 means unlimited.
        "throttle_mbps": 0,
        "throttle_presets_mbps": [5, 10, 25, 50, 0],
        "history_limit": 200,
        "chunk_bytes": 262144,
    },
    "ui": {
        "language": "en",
        "performance_mode": False,
        "overscan_percent": 5,
        "cards_per_row": 5,
        "poll_interval_ms": 1000,
        "toast_position": "bottom-right",
        "bgm_enabled": False,
        "bgm_volume": 0.4,
        "sfx_enabled": False,
        "rumble_enabled": True,
        "browser_home": "about:blank",
        "browser_bookmarks": [],
    },
    "safety": {
        # Off by default: probing PSN domains means this machine makes outbound
        # requests, which is exactly what the air-gap audit is checking for.
        "active_probing": False,
        "psn_domains": [
            "playstation.net",
            "playstation.com",
            "sonyentertainmentnetwork.com",
            "np.community.playstation.net",
            "telemetry.sonyentertainmentnetwork.com",
        ],
        "probe_timeout_seconds": 3,
        "pihole": {"enabled": False, "base_url": "", "api_token": ""},
        "log_retention_days": 7,
        # Extra substrings to exempt from the air-gap audit. XML namespace URIs are
        # exempted in code (see safety.ALWAYS_ALLOWED) rather than here, so an older
        # configs.json cannot reintroduce them as findings.
        "audit_allow_patterns": ["about:blank"],
    },
    "backup": {
        # Save-data backup bundles whatever local folders you list here into a zip.
        # Reading saves off the console itself needs a console-side file API, which
        # etaHEN does not expose by default -- see README.
        "source_dirs": [],
        "output_dir": "{app}/backups",
        "keep_archives": 10,
    },
    "wipe": {
        "pin_salt": "",
        "pin_hash": "",
        "pin_iterations": 240000,
        "confirm_phrase": "WIPE EVERYTHING",
        "max_attempts": 5,
        "lockout_minutes": 15,
        "plan_ttl_seconds": 120,
        "allowlist": [
            "{app}/logs",
            "{app}/data/transfers.json",
            "{app}/data/library.json",
            "{app}/data/browser_history.json",
            "{app}/data/session_events.json",
        ],
        # Intentionally empty. Console-side deletion only happens for paths you
        # add yourself.
        "console_paths": [],
    },
}

_lock = threading.RLock()
_cache: Dict[str, Any] | None = None
_resolved_host_ip: str | None = None

# What the process actually bound this run. ``run.py --host/--port`` set these so the
# URL handed to the console matches reality instead of what configs.json says. They
# are deliberately not persisted: an override is for one run.
_runtime_host: str | None = None
_runtime_port: int | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def detect_lan_ip() -> str:
    """Best-effort primary LAN address of this machine.

    Opens an unconnected UDP socket toward a routable address and reads back the
    local endpoint the OS picked. No packet is actually sent, so this works fully
    offline and never touches the network.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return str(probe.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        probe.close()


def expand_tokens(value: Any) -> Any:
    """Recursively expand ``{app}`` in strings. Lists and dicts are walked."""
    if isinstance(value, str):
        return value.replace("{app}", APP_ROOT.as_posix())
    if isinstance(value, list):
        return [expand_tokens(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_tokens(item) for key, item in value.items()}
    return value


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``override`` onto a copy of ``base``.

    Dicts merge key-by-key so a partial config file still receives every default.
    Lists and scalars are replaced wholesale -- a user who configures three PKG
    directories means exactly three, not three plus the default.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _strip_comments(mapping: Dict[str, Any]) -> Dict[str, Any]:
    """Drop ``_underscore`` documentation keys from a loaded config."""
    return {key: value for key, value in mapping.items() if not key.startswith("_")}


def write_atomic(path: Path, text: str) -> None:
    """Write via temp file + replace so an interrupted save cannot truncate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def load(force_reload: bool = False) -> Dict[str, Any]:
    """Return the merged, token-expanded configuration.

    Cached after the first call; pass ``force_reload=True`` after editing
    ``configs.json`` by hand.
    """
    global _cache, _resolved_host_ip
    with _lock:
        if _cache is not None and not force_reload:
            return _cache

        on_disk: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                    on_disk = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                # A broken config must not stop the hub from booting. Fall back to
                # defaults and keep the bad file aside for inspection.
                broken = CONFIG_PATH.with_name("configs.json.broken")
                try:
                    os.replace(CONFIG_PATH, broken)
                except OSError:
                    pass
                print(f"[config] configs.json unreadable ({exc}); kept as {broken.name}")
                on_disk = {}

        merged = deep_merge(DEFAULTS, _strip_comments(on_disk))

        if not CONFIG_PATH.exists():
            # First run: materialise a real config so the Settings tab has
            # somewhere to persist to.
            write_atomic(CONFIG_PATH, json.dumps(merged, indent=2) + "\n")

        expanded = expand_tokens(merged)

        configured_ip = expanded["server"]["host_ip"]
        _resolved_host_ip = detect_lan_ip() if configured_ip == "auto" else str(configured_ip)
        expanded["server"]["resolved_host_ip"] = _resolved_host_ip

        for directory in (DATA_DIR, LOG_DIR, COVERS_DIR, BGM_DIR):
            directory.mkdir(parents=True, exist_ok=True)

        _cache = expanded
        return _cache


def raw() -> Dict[str, Any]:
    """The unexpanded on-disk config merged with defaults (for saving back)."""
    with _lock:
        on_disk: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                    on_disk = json.load(handle)
            except (OSError, json.JSONDecodeError):
                on_disk = {}
        return deep_merge(DEFAULTS, _strip_comments(on_disk))


def save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``updates`` into ``configs.json`` and refresh the cache.

    ``updates`` is a nested dict mirroring the file shape, not dotted keys.
    Returns the newly loaded (expanded) config.
    """
    with _lock:
        merged = deep_merge(raw(), updates)
        write_atomic(CONFIG_PATH, json.dumps(merged, indent=2) + "\n")
        return load(force_reload=True)


def get(dotted: str, default: Any = None) -> Any:
    """Read a value by dotted path, e.g. ``get("transfers.max_concurrent")``."""
    node: Any = load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_runtime_binding(host: Any = None, port: Any = None) -> None:
    """Record the address this process actually bound.

    Called by ``run.py`` when ``--host``/``--port`` override the config. Without it,
    :func:`base_url` would advertise the configured port while the server listened
    on another one, and every install URL handed to the console would 404.

    A wildcard bind address is ignored for advertising purposes: the console cannot
    connect to ``0.0.0.0``, so the detected LAN address is kept instead.
    """
    global _runtime_host, _runtime_port
    if host:
        text = str(host)
        if text not in ("0.0.0.0", "::", "*"):
            _runtime_host = text
    if port:
        _runtime_port = int(port)


def host_ip() -> str:
    """The address the console should use to reach this hub."""
    load()
    return _runtime_host or _resolved_host_ip or "127.0.0.1"


def port() -> int:
    """The port this hub is actually listening on."""
    return int(_runtime_port or get("server.port", 8080) or 8080)


def base_url() -> str:
    """The URL the PS5 should use to reach this hub."""
    return "http://{0}:{1}".format(host_ip(), port())


def pkg_directories() -> Iterator[Path]:
    """Configured package roots that actually exist on disk."""
    for entry in get("library.pkg_directories", []) or []:
        path = Path(str(entry)).expanduser()
        if path.is_dir():
            yield path


def reset_cache() -> None:
    """Drop the cached config and any runtime binding. Used by the test suite."""
    global _cache, _runtime_host, _runtime_port
    with _lock:
        _cache = None
        _runtime_host = None
        _runtime_port = None
