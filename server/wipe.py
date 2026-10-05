"""Emergency wipe: PIN gate, lockout, dry-run planning, allowlisted deletion.

This is the one destructive feature in the hub, so it is built to be hard to fire
by accident and impossible to aim at the wrong thing:

* **PIN** -- PBKDF2-HMAC-SHA256, salted (see :mod:`server.security`). Wrong
  attempts are counted and the lockout survives a restart, because an attacker
  with physical access would otherwise just restart the hub.
* **Typed phrase** -- in addition to the PIN, ``wipe.confirm_phrase`` must be
  typed exactly. A PIN can be fat-fingered into correctness; a phrase cannot.
* **Dry run first, always** -- ``plan()`` returns the exact list of paths, their
  sizes, and everything it refused to touch. ``execute()`` only accepts a token
  issued by a plan, and that token expires.
* **Allowlist only** -- nothing outside ``wipe.allowlist`` is considered, and even
  inside it the refusal rules below are absolute: no drive root, no ancestor of
  the app, no symlink, and nothing in :data:`server.config.PROTECTED_PATHS` (the
  hub's own source). The app cannot delete itself.

Console-side paths are supported but ship empty, and are only acted on if an
``etahen.endpoints.delete_path`` endpoint is configured. Otherwise they appear in
the plan as explicitly not-deleted, with the reason.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, pkg_parser, security
from .etahen import EtaHenClient, EtaHenError
from .logs import logger, record_event

# A foolproof, accurate console-recovery checklist. Shown beside the wipe so the
# nuclear option always has a documented way back.
SAFE_MODE_GUIDE: Dict[str, Any] = {
    "title": "Safe Mode recovery",
    "intro": (
        "Safe Mode boots the console with the minimum it needs to repair itself. "
        "Work down this list: each option is more drastic than the one above it, so "
        "stop as soon as the console behaves."
    ),
    "entering": [
        "Power the console fully off - hold the power button until the indicator stops blinking, then wait for it to go dark. Rest mode is not off.",
        "Press and hold the power button again. You will hear one beep immediately; keep holding until you hear a second beep (about seven seconds), then release.",
        "Connect a DualSense with a USB cable - Safe Mode does not accept Bluetooth - and press the PS button.",
    ],
    "options": [
        {"n": 1, "name": "Restart PS5", "use": "Leaves Safe Mode and boots normally. Try this first."},
        {"n": 2, "name": "Change Video Output", "use": "For a blank or unsupported-signal screen. Does not touch data."},
        {"n": 3, "name": "Update System Software", "use": "Reinstalls/updates firmware from USB, disc or network. Note: this will remove a jailbreak, and may raise the firmware above what your exploit supports."},
        {"n": 4, "name": "Restore Default Settings", "use": "Resets settings to factory values. Games and saves are kept."},
        {"n": 5, "name": "Clear Cache and Rebuild Database", "use": "Safe and often sufficient: clears the system cache, then rebuilds the content database. Does not delete games or saves."},
        {"n": 6, "name": "Reset PS5", "use": "Deletes users, settings, games and saved data; keeps the installed system software."},
        {"n": 7, "name": "Reset PS5 (Reinstall System Software)", "use": "Erases everything including the system software, then reinstalls it from USB. This is the clean factory state."},
    ],
    "usb_reinstall": [
        "Use a USB drive (FAT32 or exFAT) with at least ~1 GB free. Everything on it stays, but back it up anyway.",
        "On the drive, create the folder path PS5\\UPDATE (exactly that capitalisation).",
        "Download the full reinstallation file from Sony's official PS5 system-software page - the reinstall file, not the smaller update file - and save it into PS5\\UPDATE as PS5UPDATE.PUP.",
        "Plug the drive into the console, enter Safe Mode, and choose option 7 (or option 3 to update rather than wipe).",
        "If the console cannot see the file, re-check the folder names, the filename, and that the drive is FAT32/exFAT rather than NTFS.",
    ],
    "cautions": [
        "Options 6 and 7 are irreversible. Copy anything you care about to USB first.",
        "Reinstalling system software updates the firmware. If you rely on a specific firmware version, confirm what the reinstall file contains before using it.",
        "Keep the console off the internet throughout if staying offline is the point of your setup - a reinstall will happily fetch the newest firmware if it can.",
    ],
}


class WipeGuard:
    """Owns the PIN gate, the lockout counter, and the plan/execute handshake."""

    def __init__(
        self,
        client: Optional[EtaHenClient] = None,
        on_wiped: Optional[Callable[[], None]] = None,
    ) -> None:
        self.client = client
        self.on_wiped = on_wiped
        self.path = config.DATA_DIR / "wipe_state.json"
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {"attempts": 0, "locked_until": 0.0, "last_attempt": 0.0}
        self._plans: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Lockout state
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(stored, dict):
            self._state.update(
                {
                    "attempts": int(stored.get("attempts") or 0),
                    "locked_until": float(stored.get("locked_until") or 0.0),
                    "last_attempt": float(stored.get("last_attempt") or 0.0),
                }
            )

    def _flush(self) -> None:
        try:
            config.write_atomic(self.path, json.dumps(self._state, indent=1) + "\n")
        except OSError as exc:
            logger.error("Could not persist wipe lockout state: %s", exc)

    def locked_for(self) -> float:
        with self._lock:
            return max(0.0, float(self._state["locked_until"]) - time.time())

    def _register_failure(self) -> Dict[str, Any]:
        limit = max(1, int(config.get("wipe.max_attempts", 5) or 5))
        minutes = max(1, int(config.get("wipe.lockout_minutes", 15) or 15))
        with self._lock:
            self._state["attempts"] = int(self._state["attempts"]) + 1
            self._state["last_attempt"] = time.time()
            if self._state["attempts"] >= limit:
                self._state["locked_until"] = time.time() + minutes * 60
                self._state["attempts"] = 0
                locked = True
            else:
                locked = False
            self._flush()
            remaining = max(0, limit - int(self._state["attempts"])) if not locked else 0
        record_event(
            "wipe.attempt_failed",
            "Emergency wipe authentication failed" + (" - locked out" if locked else ""),
            locked=locked,
        )
        return {"locked": locked, "attempts_remaining": remaining, "locked_seconds": self.locked_for()}

    def _register_success(self) -> None:
        with self._lock:
            self._state["attempts"] = 0
            self._state["locked_until"] = 0.0
            self._state["last_attempt"] = time.time()
            self._flush()

    def authenticate(self, pin: str, phrase: str) -> Tuple[bool, Dict[str, Any]]:
        """Check PIN + phrase, honouring the lockout. Never reveals which failed."""
        remaining_lock = self.locked_for()
        if remaining_lock > 0:
            return False, {
                "error": "locked_out",
                "message": f"Locked for another {int(remaining_lock)}s after too many failed attempts.",
                "locked_seconds": int(remaining_lock),
            }

        if not security.pin_is_configured():
            return False, {"error": "no_pin", "message": "Set an emergency wipe PIN first."}

        expected_phrase = str(config.get("wipe.confirm_phrase", "") or "")
        pin_ok = security.verify_pin(
            str(pin or ""),
            str(config.get("wipe.pin_salt", "")),
            str(config.get("wipe.pin_hash", "")),
            int(config.get("wipe.pin_iterations", 240000) or 240000),
        )
        phrase_ok = str(phrase or "") == expected_phrase

        if pin_ok and phrase_ok:
            self._register_success()
            return True, {}

        failure = self._register_failure()
        return False, {
            "error": "bad_credentials",
            "message": "PIN or confirmation phrase incorrect.",
            **failure,
        }

    # ------------------------------------------------------------------ #
    # Target resolution
    # ------------------------------------------------------------------ #

    @staticmethod
    def _refusal_reason(path: Path) -> Optional[str]:
        """Why this path must not be deleted, or ``None`` if it is acceptable.

        The rules are intentionally blunt. A wipe routine that is clever about
        edge cases is a wipe routine that eventually deletes the wrong thing.
        """
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            return f"cannot resolve ({exc})"

        if path.is_symlink():
            return "is a symlink or junction (refused: could point anywhere)"

        if resolved == Path(resolved.anchor):
            return "is a filesystem root"

        app_root = config.APP_ROOT.resolve()
        if resolved == app_root:
            return "is the hub's own folder"
        if resolved in app_root.parents:
            return "is an ancestor of the hub's folder"

        for protected in config.PROTECTED_PATHS:
            try:
                protected_resolved = protected.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved == protected_resolved or protected_resolved in resolved.parents:
                return f"is inside the hub's own source ({protected.name})"
            if resolved in protected_resolved.parents:
                return f"contains the hub's own source ({protected.name})"

        home = Path.home().resolve()
        if resolved == home:
            return "is your home folder"

        for sensitive in ("windows", "program files", "program files (x86)", "programdata"):
            parts = [part.lower() for part in resolved.parts]
            if sensitive in parts:
                return f"is inside a system directory ({sensitive})"

        return None

    @staticmethod
    def _measure(path: Path) -> Tuple[int, int]:
        """``(file_count, total_bytes)`` for a file or a directory tree."""
        if path.is_file():
            try:
                return 1, path.stat().st_size
            except OSError:
                return 1, 0
        count = 0
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                count += 1
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        return count, total

    def resolve_targets(self) -> Dict[str, List[Dict[str, Any]]]:
        """Split the configured allowlist into actionable, missing, and refused."""
        actionable: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        refused: List[Dict[str, Any]] = []

        for raw_entry in config.get("wipe.allowlist", []) or []:
            path = Path(str(raw_entry)).expanduser()
            reason = self._refusal_reason(path)
            if reason:
                refused.append({"path": str(path), "reason": reason})
                continue
            if not path.exists():
                missing.append({"path": str(path), "reason": "does not exist"})
                continue
            count, size = self._measure(path)
            actionable.append(
                {
                    "path": str(path),
                    "kind": "directory" if path.is_dir() else "file",
                    "file_count": count,
                    "size_bytes": size,
                    "size_label": pkg_parser.format_size(size),
                }
            )

        console_paths = [str(entry) for entry in (config.get("wipe.console_paths", []) or [])]
        endpoints = config.get("etahen.endpoints", {}) or {}
        console_supported = "delete_path" in endpoints
        console_entries = [
            {
                "path": entry,
                "supported": console_supported,
                "reason": None
                if console_supported
                else "No etahen.endpoints.delete_path is configured, so console-side paths are not touched.",
            }
            for entry in console_paths
        ]

        return {
            "actionable": actionable,
            "missing": missing,
            "refused": refused,
            "console": console_entries,
        }

    # ------------------------------------------------------------------ #
    # Plan / execute
    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, Any]:
        targets = self.resolve_targets()
        limit = max(1, int(config.get("wipe.max_attempts", 5) or 5))
        with self._lock:
            attempts = int(self._state["attempts"])
        return {
            "pin_configured": security.pin_is_configured(),
            "confirm_phrase": str(config.get("wipe.confirm_phrase", "") or ""),
            "attempts_used": attempts,
            "attempts_remaining": max(0, limit - attempts),
            "locked_seconds": int(self.locked_for()),
            "allowlist": config.get("wipe.allowlist", []),
            "console_paths": config.get("wipe.console_paths", []),
            "targets": targets,
            "total_bytes": sum(item["size_bytes"] for item in targets["actionable"]),
            "total_label": pkg_parser.format_size(
                sum(item["size_bytes"] for item in targets["actionable"])
            ),
            "protected": [str(path) for path in config.PROTECTED_PATHS],
            "iterations": int(config.get("wipe.pin_iterations", 240000) or 240000),
        }

    def plan(self, pin: str, phrase: str) -> Dict[str, Any]:
        """Authenticate and return a dry run plus a short-lived execution token."""
        ok, failure = self.authenticate(pin, phrase)
        if not ok:
            return {"ok": False, **failure}

        targets = self.resolve_targets()
        ttl = max(15, int(config.get("wipe.plan_ttl_seconds", 120) or 120))
        token = secrets.token_urlsafe(24)
        expires_at = time.time() + ttl

        with self._lock:
            # Only one plan may be live at a time.
            self._plans = {token: {"targets": targets, "expires_at": expires_at}}

        total = sum(item["size_bytes"] for item in targets["actionable"])
        record_event(
            "wipe.planned",
            "Wipe dry run: {0} target(s), {1}".format(
                len(targets["actionable"]), pkg_parser.format_size(total)
            ),
        )
        return {
            "ok": True,
            "token": token,
            "expires_at": expires_at,
            "expires_in": ttl,
            "targets": targets,
            "total_bytes": total,
            "total_label": pkg_parser.format_size(total),
            "note": (
                "Nothing has been deleted. Review the list, then confirm to execute. "
                "The token expires in {0}s.".format(ttl)
            ),
        }

    def _delete_path(self, path: Path) -> Dict[str, Any]:
        """Delete one allowlisted target. Directories are emptied, not removed.

        Keeping the directory itself means the hub can keep logging after a wipe,
        and that a path in your allowlist does not silently stop existing.
        Files held open by this process (the current log) are truncated instead,
        which is reported distinctly rather than as a success.
        """
        removed_files = 0
        truncated_files = 0
        freed = 0
        errors: List[str] = []

        def handle_file(target: Path) -> None:
            nonlocal removed_files, truncated_files, freed
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            try:
                target.unlink()
                removed_files += 1
                freed += size
            except PermissionError:
                try:
                    with open(target, "w", encoding="utf-8"):
                        pass
                    truncated_files += 1
                    freed += size
                except OSError as exc:
                    errors.append(f"{target}: {exc}")
            except OSError as exc:
                errors.append(f"{target}: {exc}")

        if path.is_file():
            handle_file(path)
        else:
            for root, dirs, files in os.walk(path, topdown=False):
                for name in files:
                    handle_file(Path(root) / name)
                for name in dirs:
                    directory = Path(root) / name
                    try:
                        directory.rmdir()
                    except OSError as exc:
                        errors.append(f"{directory}: {exc}")

        return {
            "path": str(path),
            "removed_files": removed_files,
            "truncated_files": truncated_files,
            "freed_bytes": freed,
            "freed_label": pkg_parser.format_size(freed),
            "errors": errors,
        }

    def _delete_console_paths(self) -> List[Dict[str, Any]]:
        """Ask the console to delete its own allowlisted paths, if supported."""
        endpoints = config.get("etahen.endpoints", {}) or {}
        console_paths = [str(entry) for entry in (config.get("wipe.console_paths", []) or [])]
        if not console_paths:
            return []
        if "delete_path" not in endpoints or self.client is None:
            return [
                {
                    "path": entry,
                    "deleted": False,
                    "reason": "No etahen.endpoints.delete_path configured; nothing was sent to the console.",
                }
                for entry in console_paths
            ]

        results: List[Dict[str, Any]] = []
        for entry in console_paths:
            try:
                response = self.client.call(
                    "delete_path",
                    tokens={"path": entry},
                    payload_override={"path": entry},
                )
                results.append({"path": entry, "deleted": response.get("ok", True), "response": response})
            except EtaHenError as exc:
                results.append({"path": entry, "deleted": False, "reason": str(exc)})
        return results

    def execute(self, token: str, stop_server: bool = True) -> Dict[str, Any]:
        """Carry out a previously planned wipe. Requires a live plan token."""
        with self._lock:
            plan = self._plans.get(str(token))
            if plan is None:
                return {"ok": False, "error": "unknown_token", "message": "Run a dry run first."}
            if time.time() > float(plan["expires_at"]):
                self._plans.pop(str(token), None)
                return {"ok": False, "error": "expired_token", "message": "The plan expired. Run the dry run again."}
            self._plans.pop(str(token), None)
            targets = plan["targets"]

        results = [self._delete_path(Path(item["path"])) for item in targets["actionable"]]
        console_results = self._delete_console_paths()

        # Drop in-memory copies of what was just deleted from disk, so the UI does
        # not keep showing wiped history until a restart.
        if self.on_wiped is not None:
            try:
                self.on_wiped()
            except Exception as exc:  # pragma: no cover - cleanup must not mask the wipe
                logger.error("Post-wipe cleanup hook failed: %s", exc)

        freed = sum(item["freed_bytes"] for item in results)
        errors = [error for item in results for error in item["errors"]]
        report = {
            "ok": True,
            "results": results,
            "console": console_results,
            "freed_bytes": freed,
            "freed_label": pkg_parser.format_size(freed),
            "removed_files": sum(item["removed_files"] for item in results),
            "truncated_files": sum(item["truncated_files"] for item in results),
            "errors": errors,
            "server_stopping": bool(stop_server),
            "note": (
                "The hub's own source was not touched and cannot be - see the protected list. "
                "Restart with start_hub.bat when you want it back."
            ),
        }
        record_event(
            "wipe.executed",
            "Emergency wipe executed: {0} file(s) removed, {1} freed".format(
                report["removed_files"], report["freed_label"]
            ),
            errors=len(errors),
        )
        logger.warning("EMERGENCY WIPE EXECUTED - %s freed, %d error(s)", report["freed_label"], len(errors))

        if stop_server:
            self.request_shutdown()
        return report

    @staticmethod
    def request_shutdown(delay_seconds: float = 1.5) -> None:
        """Stop the process shortly after the response has been delivered.

        The hub deliberately does not delete its own files, so this is a stop, not
        a self-destruct: ``start_hub.bat`` brings it back.
        """

        def _stop() -> None:
            time.sleep(delay_seconds)
            logger.warning("Shutting down after emergency wipe")
            for handler in list(logger.handlers):
                try:
                    handler.flush()
                except Exception:  # pragma: no cover
                    pass
            os._exit(0)

        threading.Thread(target=_stop, name="post-wipe-shutdown", daemon=True).start()
