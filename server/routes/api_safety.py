"""Safety endpoints: killswitch state, air-gap audit, optional probing, checklist."""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, jsonify, request

from .. import config
from ..hub import hub
from ..logs import record_event

bp = Blueprint("api_safety", __name__, url_prefix="/api/safety")


@bp.get("")
def state():
    """The full panel: verified facts, unverified caveats, warnings, checklist."""
    return jsonify({"ok": True, **hub().safety.state()})


@bp.post("/audit")
def audit():
    """Re-run the air-gap audit over the shipped frontend."""
    result = hub().safety.audit_sources(force=True)
    record_event(
        "safety.audit",
        "Air-gap audit re-run: {0} ({1} blocking)".format(
            "pass" if result["pass"] else "FAIL", result["blocking_count"]
        ),
    )
    return jsonify(
        {
            "ok": True,
            "audit": result,
            "message": (
                "Audit passed: no remote references in the shipped UI."
                if result["pass"]
                else "Audit failed: {0} blocking remote reference(s).".format(result["blocking_count"])
            ),
        }
    )


@bp.post("/probe")
def probe():
    """Run the PSN reachability probe (requires ``safety.active_probing``)."""
    result = hub().safety.probe_psn()
    if not result.get("enabled"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "probing_disabled",
                    "message": result.get("message"),
                    "probe": result,
                }
            ),
            409,
        )
    reachable = result.get("reachable") or []
    return jsonify(
        {
            "ok": True,
            "probe": result,
            "message": (
                "No probed PSN host is reachable from this PC."
                if not reachable
                else "Reachable from this PC: " + ", ".join(reachable)
            ),
        }
    )


@bp.post("/probing")
def set_probing():
    """Turn active probing on or off, with the trade-off stated in the reply."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    config.save({"safety": {"active_probing": enabled}})
    return jsonify(
        {
            "ok": True,
            "active_probing": enabled,
            "message": (
                "Active probing on. This machine will make outbound requests when you run a probe."
                if enabled
                else "Active probing off. No outbound requests will be made."
            ),
        }
    )


@bp.post("/checklist")
def checklist():
    """Tick or untick one hardening step."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    item_id = str(payload.get("id") or "")
    try:
        items = hub().safety.set_checklist_item(item_id, bool(payload.get("checked")))
    except KeyError:
        return jsonify({"ok": False, "error": "unknown_item", "message": "No such checklist item."}), 404
    done = sum(1 for item in items if item["checked"])
    return jsonify({"ok": True, "checklist": items, "done": done, "total": len(items)})
