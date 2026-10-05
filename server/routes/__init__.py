"""JSON blueprints. Each module owns one area of the API surface.

Shared conventions, so the frontend only needs one response handler:

* success -> ``{"ok": true, ...}``
* failure -> ``{"ok": false, "error": "<slug>", "message": "<human text>"}`` with a
  4xx/5xx status. The slug is for code, the message is for the toast.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from flask import Flask

from .api_browser import bp as browser_bp
from .api_library import bp as library_bp
from .api_safety import bp as safety_bp
from .api_system import bp as system_bp
from .api_transfers import bp as transfers_bp
from .api_wipe import bp as wipe_bp

BLUEPRINTS = (library_bp, transfers_bp, system_bp, safety_bp, wipe_bp, browser_bp)


def register(app: Flask) -> None:
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)


def fail(error: str, message: str, status: int = 400, **extra: Any) -> Tuple[Dict[str, Any], int]:
    """Build a uniform error body."""
    return {"ok": False, "error": error, "message": message, **extra}, status
