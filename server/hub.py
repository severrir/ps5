"""The service container.

One object owns the long-lived services (etaHEN client, watchdog, library scanner,
transfer queue, safety panel, wipe guard) and their start/stop order. Blueprints
reach it through :func:`hub`, which reads it off ``current_app.extensions`` -- that
indirection is what keeps the route modules free of imports from the package root
and therefore free of circular imports.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import current_app

from . import config, telemetry
from .etahen import EtaHenClient, Watchdog
from .library import Library
from .logs import logger, record_event
from .safety import SafetyPanel
from .transfers import TransferQueue
from .wipe import WipeGuard


class Hub:
    """Wires the services together and owns their lifecycle."""

    def __init__(self) -> None:
        self.client = EtaHenClient()
        self.watchdog = Watchdog(self.client)
        self.library = Library()
        self.transfers = TransferQueue(self.library, self.client)
        self.safety = SafetyPanel(self.client)
        self.wipe = WipeGuard(self.client, on_wiped=self._after_wipe)
        self._started = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start background threads and run the startup self-audit."""
        if self._started:
            return
        self._started = True
        self.library.start()
        self.transfers.start()
        self.watchdog.start()
        audit = self.safety.audit_sources(force=True)
        record_event(
            "safety.audit",
            "Startup air-gap audit: {0} ({1} file(s) scanned)".format(
                "pass" if audit["pass"] else "FAIL", audit["files_scanned"]
            ),
            blocking=audit["blocking_count"],
        )
        logger.info("Hub ready at %s (etaHEN mode: %s)", config.base_url(), self.client.mode())

    def stop(self) -> None:
        self.library.stop()
        self.transfers.stop()
        self.watchdog.stop()
        self._started = False

    def _after_wipe(self) -> None:
        """Clear in-memory state whose on-disk copy was just deleted."""
        from . import logs as logs_module

        self.transfers.clear_all()
        self.library.store.clear()
        self.safety.clear_checklist()
        logs_module.clear_events()

    # ------------------------------------------------------------------ #
    # Aggregate state for the UI's single polling endpoint
    # ------------------------------------------------------------------ #

    def state(self) -> Dict[str, Any]:
        """One response with everything the dashboard polls for.

        The PS5 browser is happiest with few requests, so the UI polls this once a
        second instead of hitting six endpoints.
        """
        return {
            "ok": True,
            "version": config.get("version", "1.0.0"),
            "base_url": config.base_url(),
            "console": self.client.state(),
            "library": self.library.status(),
            "transfers": self.transfers.snapshot(),
            "storage": telemetry.storage(self.client),
            "hardware": telemetry.hardware(self.client),
            "safety_level": self.safety.quick_level(),
            "ui": config.get("ui", {}),
        }


def hub() -> Hub:
    """The Hub for the active application."""
    return current_app.extensions["hub"]
