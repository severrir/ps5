"""Emergency wipe endpoints. PIN-gated, dry-run-first, allowlist-only.

The flow the UI must follow, and the only one the server accepts:

1. ``GET  /api/wipe/status``   -- is a PIN set, what would be targeted, am I locked out
2. ``POST /api/wipe/pin``      -- set or change the PIN (changing requires the old one)
3. ``POST /api/wipe/plan``     -- PIN + phrase; returns the exact file list and a token
4. ``POST /api/wipe/execute``  -- that token; deletes, then stops the hub

There is no endpoint that deletes without a plan token, and tokens expire.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, jsonify, request

from .. import config, security
from ..hub import hub
from ..wipe import SAFE_MODE_GUIDE

bp = Blueprint("api_wipe", __name__, url_prefix="/api/wipe")

MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 32


@bp.get("/status")
def status():
    return jsonify({"ok": True, **hub().wipe.status()})


@bp.get("/safe-mode")
def safe_mode():
    """The Safe Mode recovery guide. Static text, no console interaction."""
    return jsonify({"ok": True, "guide": SAFE_MODE_GUIDE})


@bp.post("/pin")
def set_pin():
    """Set the PIN, or change it by proving the current one."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    new_pin = str(payload.get("pin") or "")

    if not (MIN_PIN_LENGTH <= len(new_pin) <= MAX_PIN_LENGTH):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "bad_pin",
                    "message": f"PIN must be {MIN_PIN_LENGTH}-{MAX_PIN_LENGTH} characters.",
                }
            ),
            400,
        )

    if security.pin_is_configured():
        remaining_lock = hub().wipe.locked_for()
        if remaining_lock > 0:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "locked_out",
                        "message": f"Locked for another {int(remaining_lock)}s.",
                    }
                ),
                429,
            )
        current = str(payload.get("current_pin") or "")
        valid = security.verify_pin(
            current,
            str(config.get("wipe.pin_salt", "")),
            str(config.get("wipe.pin_hash", "")),
            int(config.get("wipe.pin_iterations", 240000) or 240000),
        )
        if not valid:
            hub().wipe._register_failure()  # noqa: SLF001 - same lockout counter by design
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "bad_current_pin",
                        "message": "Current PIN is incorrect.",
                    }
                ),
                403,
            )

    result = security.set_pin(new_pin)
    return jsonify(
        {
            "ok": True,
            **result,
            "message": "PIN saved. It is stored only as a salted PBKDF2-SHA256 hash.",
        }
    )


@bp.post("/plan")
def plan():
    """Authenticate and return the dry run. Nothing is deleted here."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    result = hub().wipe.plan(str(payload.get("pin") or ""), str(payload.get("phrase") or ""))
    if not result.get("ok"):
        status_code = {
            "locked_out": 429,
            "no_pin": 409,
            "bad_credentials": 403,
        }.get(str(result.get("error")), 400)
        return jsonify(result), status_code
    return jsonify(result)


@bp.post("/execute")
def execute():
    """Carry out a planned wipe, then stop the hub unless told otherwise."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "")
    stop_server = payload.get("stop_server")
    stop_server = True if stop_server is None else bool(stop_server)

    result = hub().wipe.execute(token, stop_server=stop_server)
    if not result.get("ok"):
        return jsonify(result), 409
    return jsonify(result)
