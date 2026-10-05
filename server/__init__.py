"""Application factory for the PS5 Command Hub.

Builds the Flask app, installs the security middleware, mounts the blueprints, and
wires the three non-API routes that matter: the single-page shell, the package file
server the console pulls from, and the local media folders (covers, BGM).

Threading: the WSGI server runs threaded (see ``run.py``) because a package
transfer occupies its worker thread for the duration of the install. Background work
-- directory scanning, the transfer worker, the etaHEN watchdog -- lives in daemon
threads owned by :class:`server.hub.Hub`.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import config, fileserve, mock_etahen, routes, security
from .hub import Hub
from .logs import logger, setup as setup_logging

__all__ = ["create_app"]


def create_app(start_background: bool = True) -> Flask:
    """Build and return the configured application.

    ``start_background=False`` keeps the scanner, worker and watchdog threads from
    starting, which is what the test suite wants.
    """
    config.load()
    setup_logging()

    app = Flask(
        __name__,
        static_folder=str(config.APP_ROOT / "static"),
        static_url_path="/static",
        template_folder=str(config.APP_ROOT / "templates"),
    )
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # JSON bodies only; nothing is uploaded

    security.install(app)

    hub = Hub()
    app.extensions["hub"] = hub
    routes.register(app)

    # ------------------------------------------------------------------ #
    # Shell
    # ------------------------------------------------------------------ #

    @app.get("/")
    def index() -> str:
        ui = config.get("ui", {}) or {}
        return render_template(
            "index.html",
            version=config.get("version", "1.0.0"),
            base_url=config.base_url(),
            overscan=float(ui.get("overscan_percent", 5) or 0),
            cards_per_row=int(ui.get("cards_per_row", 5) or 5),
            performance_mode="1" if ui.get("performance_mode") else "0",
            language=str(ui.get("language", "en")),
            poll_interval=int(ui.get("poll_interval_ms", 1000) or 1000),
            toast_position=str(ui.get("toast_position", "bottom-right")),
        )

    @app.get("/api/ping")
    def ping():
        return jsonify({"ok": True, "version": config.get("version", "1.0.0")})

    # ------------------------------------------------------------------ #
    # Package serving -- this is the URL handed to the console
    # ------------------------------------------------------------------ #

    @app.get("/pkg/<item_id>")
    def serve_package(item_id: str) -> Response:
        """Stream a package to the console, throttled, with Range support.

        A paused transfer answers 503 on purpose: cutting the byte stream is how
        pause is implemented, and the console retries (with a Range header if it
        supports one) when the transfer is resumed.
        """
        item = hub.library.item(item_id)
        if item is None or not item.get("path"):
            return Response("unknown package", status=404, mimetype="text/plain")

        if hub.transfers.is_paused_item(item_id):
            response = Response("transfer paused", status=503, mimetype="text/plain")
            response.headers["Retry-After"] = "5"
            return response

        def on_bytes(count: int) -> None:
            hub.transfers.note_bytes(item_id, count)

        from pathlib import Path

        return fileserve.send_package_file(
            Path(str(item["path"])),
            download_name=item.get("filename"),
            on_bytes=on_bytes,
        )

    # ------------------------------------------------------------------ #
    # Local media
    # ------------------------------------------------------------------ #

    @app.get("/covers/<path:filename>")
    def cover(filename: str):
        """Cover art from the local ``covers/`` folder. No remote art service."""
        return send_from_directory(config.COVERS_DIR, filename, max_age=3600)

    @app.get("/bgm/<path:filename>")
    def bgm(filename: str):
        """Background audio from the local ``bgm/`` folder."""
        return send_from_directory(config.BGM_DIR, filename, max_age=3600, conditional=True)

    # ------------------------------------------------------------------ #
    # Errors -- JSON for the API, the shell for everything else
    # ------------------------------------------------------------------ #

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        if request.path.startswith("/api/") or request.path.startswith("/pkg/"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (exc.name or "error").lower().replace(" ", "_"),
                        "message": exc.description,
                    }
                ),
                exc.code or 500,
            )
        return exc

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        logger.exception("Unhandled error on %s: %s", request.path, exc)
        if request.path.startswith("/api/") or request.path.startswith("/pkg/"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "internal_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                ),
                500,
            )
        raise exc

    # ------------------------------------------------------------------ #
    # Background services
    # ------------------------------------------------------------------ #

    if start_background:
        if hub.client.mode() == "mock":
            mock_etahen.start_in_process()
            logger.info("Mock etaHEN daemons started (etahen.mode is 'mock')")
        hub.start()
    else:
        # Still run the audit so tests and one-shot checks see a populated panel.
        hub.safety.audit_sources(force=True)

    return app


def hub_of(app: Flask) -> Any:
    """Convenience accessor for scripts and tests."""
    return app.extensions["hub"]
