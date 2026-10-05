"""Queue endpoints: enqueue, reorder, pause/resume, throttle, history."""

from __future__ import annotations

from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from .. import config, fileserve
from ..hub import hub

bp = Blueprint("api_transfers", __name__, url_prefix="/api/transfers")


@bp.get("")
def snapshot():
    return jsonify({"ok": True, **hub().transfers.snapshot()})


@bp.post("")
def enqueue():
    """Queue one item (``item_id``) or a batch (``item_ids``)."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    ids: List[str] = []
    if payload.get("item_id"):
        ids = [str(payload["item_id"])]
    elif isinstance(payload.get("item_ids"), list):
        ids = [str(value) for value in payload["item_ids"]]

    if not ids:
        return jsonify({"ok": False, "error": "no_items", "message": "Nothing to queue."}), 400

    result = hub().transfers.enqueue_many(ids)
    if result["missing"] and not result["added"]:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "unknown_item",
                    "message": "No matching library item.",
                    "missing": result["missing"],
                }
            ),
            404,
        )

    count = len(result["added"])
    return jsonify(
        {
            "ok": True,
            "queued": count,
            "missing": result["missing"],
            "message": "Queued {0} package{1}.".format(count, "" if count == 1 else "s"),
            **hub().transfers.snapshot(),
        }
    )


def _act(transfer_id: str, action: str):
    queue = hub().transfers
    handler = {
        "pause": queue.pause,
        "resume": queue.resume,
        "cancel": queue.cancel,
        "retry": queue.retry,
    }[action]
    entry = handler(transfer_id)
    if entry is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "not_applicable",
                    "message": f"Cannot {action} that transfer in its current state.",
                }
            ),
            409,
        )
    return jsonify({"ok": True, "transfer": entry, **queue.snapshot()})


@bp.post("/<transfer_id>/pause")
def pause(transfer_id: str):
    return _act(transfer_id, "pause")


@bp.post("/<transfer_id>/resume")
def resume(transfer_id: str):
    return _act(transfer_id, "resume")


@bp.post("/<transfer_id>/cancel")
def cancel(transfer_id: str):
    return _act(transfer_id, "cancel")


@bp.post("/<transfer_id>/retry")
def retry(transfer_id: str):
    return _act(transfer_id, "retry")


@bp.post("/<transfer_id>/move")
def move(transfer_id: str):
    """Reorder priority. ``delta`` of -1 is one place earlier (D-pad up)."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    try:
        delta = int(payload.get("delta", -1))
    except (TypeError, ValueError):
        delta = -1
    if not hub().transfers.move(transfer_id, delta):
        return jsonify({"ok": False, "error": "not_moved", "message": "Already at that position."}), 409
    return jsonify({"ok": True, **hub().transfers.snapshot()})


@bp.delete("/<transfer_id>")
def remove(transfer_id: str):
    if not hub().transfers.remove(transfer_id):
        return jsonify({"ok": False, "error": "unknown_transfer", "message": "No such transfer."}), 404
    return jsonify({"ok": True, **hub().transfers.snapshot()})


@bp.post("/clear-finished")
def clear_finished():
    removed = hub().transfers.clear_finished()
    return jsonify(
        {
            "ok": True,
            "removed": removed,
            "message": "Cleared {0} finished entr{1}.".format(removed, "y" if removed == 1 else "ies"),
            **hub().transfers.snapshot(),
        }
    )


@bp.post("/throttle")
def throttle():
    """Set the transfer ceiling in MB/s. ``0`` means unlimited."""
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    try:
        mbps = float(payload.get("mbps", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad_value", "message": "Throttle must be a number."}), 400

    rate = fileserve.set_throttle_mbps(mbps)
    return jsonify(
        {
            "ok": True,
            "throttle_mbps": mbps,
            "rate_bytes_per_sec": rate,
            "message": "Transfer limit: {0}".format("unlimited" if mbps <= 0 else f"{mbps:g} MB/s"),
        }
    )


@bp.post("/concurrency")
def concurrency():
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    try:
        value = max(1, min(8, int(payload.get("value", 1))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad_value", "message": "Concurrency must be a number."}), 400
    config.save({"transfers": {"max_concurrent": value}})
    return jsonify({"ok": True, "max_concurrent": value, "message": f"Installing {value} at a time."})


@bp.get("/history")
def history():
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200))))
    except ValueError:
        limit = 200
    return jsonify({"ok": True, "history": hub().transfers.history(limit)})
