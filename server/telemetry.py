"""Storage meter and hardware health, from the console when it will say, from
this PC always.

Both readings are cached for a couple of seconds because the UI polls once per
second and the console should not be asked that often. Anything the console does
not expose is reported as unavailable with the reason, never filled in with a
plausible-looking number.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from cachetools import TTLCache

from . import config, pkg_parser
from .etahen import EtaHenClient, EtaHenError

_cache: TTLCache = TTLCache(maxsize=8, ttl=2.5)


def _volume(total: int, free: int, label: str, source: str) -> Dict[str, Any]:
    total = max(0, int(total))
    free = max(0, min(int(free), total))
    used = total - free
    return {
        "label": label,
        "source": source,
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": used,
        "total_label": pkg_parser.format_size(total),
        "free_label": pkg_parser.format_size(free),
        "used_label": pkg_parser.format_size(used),
        "used_percent": round(100.0 * used / total, 1) if total else 0.0,
    }


def _normalise_console_volume(raw: Any, fallback_label: str) -> Optional[Dict[str, Any]]:
    """Accept the several shapes a daemon might use for a volume reading."""
    if not isinstance(raw, dict):
        return None
    total = raw.get("total_bytes") or raw.get("total") or raw.get("size")
    free = raw.get("free_bytes") or raw.get("free") or raw.get("available")
    if total is None or free is None:
        return None
    try:
        return _volume(int(total), int(free), str(raw.get("label") or fallback_label), "console")
    except (TypeError, ValueError):
        return None


def host_volumes() -> List[Dict[str, Any]]:
    """Disk usage for each distinct drive holding a configured package folder."""
    volumes: List[Dict[str, Any]] = []
    seen: set[str] = set()
    directories: List[Path] = list(config.pkg_directories()) or [config.APP_ROOT]
    for directory in directories:
        try:
            anchor = Path(directory).resolve().anchor or str(directory)
        except OSError:
            anchor = str(directory)
        if anchor in seen:
            continue
        seen.add(anchor)
        try:
            usage = shutil.disk_usage(str(directory))
        except OSError:
            continue
        volumes.append(_volume(usage.total, usage.free, f"PC drive {anchor}", "host"))
    return volumes


def storage(client: EtaHenClient) -> Dict[str, Any]:
    """Console volumes (when exposed) plus this PC's volumes."""
    cached = _cache.get("storage")
    if cached is not None:
        return cached

    console: Dict[str, Any] = {"available": False, "volumes": [], "reason": None}
    try:
        payload = client.storage()
        volumes = []
        for key, fallback in (("internal", "Internal SSD"), ("external", "Extended Storage")):
            normalised = _normalise_console_volume(payload.get(key), fallback)
            if normalised:
                volumes.append(normalised)
        if not volumes and isinstance(payload.get("volumes"), list):
            for index, raw in enumerate(payload["volumes"]):
                normalised = _normalise_console_volume(raw, f"Volume {index + 1}")
                if normalised:
                    volumes.append(normalised)
        if volumes:
            console = {"available": True, "volumes": volumes, "simulated": bool(payload.get("simulated"))}
        else:
            console["reason"] = "The storage endpoint answered but exposed no recognisable volume figures."
    except EtaHenError as exc:
        console["reason"] = str(exc)

    result = {"console": console, "host": host_volumes(), "checked_at": time.time()}
    _cache["storage"] = result
    return result


def hardware(client: EtaHenClient) -> Dict[str, Any]:
    """SoC temperature / fan / load, if the control daemon exposes them."""
    cached = _cache.get("hardware")
    if cached is not None:
        return cached

    try:
        payload = client.hardware()
    except EtaHenError as exc:
        result = {"available": False, "reason": str(exc), "metrics": []}
        _cache["hardware"] = result
        return result

    metrics: List[Dict[str, Any]] = []
    definitions = (
        ("soc_temp_c", "SoC temperature", "degC", 0, 100),
        ("cpu_temp_c", "CPU temperature", "degC", 0, 100),
        ("fan_rpm", "Fan speed", "rpm", 0, 3500),
        ("cpu_load_percent", "CPU load", "%", 0, 100),
        ("gpu_load_percent", "GPU load", "%", 0, 100),
    )
    for key, label, unit, low, high in definitions:
        if payload.get(key) is None:
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            continue
        span = high - low or 1
        metrics.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "value": value,
                "percent": round(max(0.0, min(100.0, 100.0 * (value - low) / span)), 1),
            }
        )

    result = {
        "available": bool(metrics),
        "reason": None if metrics else "The control daemon exposed no recognised hardware metrics.",
        "metrics": metrics,
        "uptime_seconds": payload.get("uptime_seconds"),
        "simulated": bool(payload.get("simulated")),
    }
    _cache["hardware"] = result
    return result
