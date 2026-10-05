"""The PSN killswitch panel: what is verified, what is not, and what you must do.

This module is deliberately conservative about what it claims. It reports three
different grades of information and never blurs them:

* **Verified** -- machine-checked facts: which address the hub bound to, whether
  the shipped frontend contains any remote reference, what the console's etaHEN
  heartbeat is doing, and -- only if you opt in -- whether PSN hostnames resolve
  and accept connections from this PC.
* **Unverified** -- things no local dashboard can know, stated as such. Chiefly:
  whether the console itself can reach PSN. The hub talks to etaHEN, not to the
  console's routing table.
* **Your actions** -- a persisted checklist of console- and router-side settings
  that actually determine exposure.

There is no "guaranteed ban-safe" state, because no software can honestly produce
one. The panel's highest level is "all local checks pass", which is a different
and true statement.

Active probing is **off by default**: resolving PSN hostnames would make this
machine emit the very outbound traffic the air-gap audit exists to detect.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from . import config
from .logs import logger, record_event

# Files the frontend actually ships. A remote reference in any of these breaks
# the air-gap claim, so they are the audit's scope.
AUDIT_DIRECTORIES = ("static", "templates")
AUDIT_EXTENSIONS = (".html", ".css", ".js", ".json", ".svg")

URL_RE = re.compile(r"https?://[^\s\"'`<>()\\]+", re.IGNORECASE)
PROTOCOL_RELATIVE_RE = re.compile(r"""(?:src|href)\s*=\s*["']//[^"']+""", re.IGNORECASE)
LOADER_HINT_RE = re.compile(
    r"(?:<script[^>]*src|<link[^>]*href|@import|url\(|fetch\(|XMLHttpRequest|new\s+Image\(|@font-face)",
    re.IGNORECASE,
)

# XML namespace identifiers. Every inline SVG must carry these and nothing ever
# fetches them, so they are exempted in code rather than through
# ``safety.audit_allow_patterns`` -- that way an existing configs.json written before
# this exemption existed cannot reintroduce them as findings, and a user cannot
# accidentally delete the exemption while editing their own allowlist.
ALWAYS_ALLOWED = (
    "www.w3.org/2000/svg",
    "www.w3.org/1999/xlink",
    "www.w3.org/1999/xhtml",
)

CHECKLIST_ITEMS: List[Dict[str, str]] = [
    {
        "id": "psn_signed_out",
        "label": "Signed out of PSN on the console",
        "detail": "Settings > Users and Accounts > Other > Sign Out. A signed-out console cannot sync trophies or activity.",
    },
    {
        "id": "auto_signin_off",
        "label": "Automatic sign-in to PSN disabled",
        "detail": "Settings > Users and Accounts > Login Settings. This is the setting that silently reconnects you after a reboot.",
    },
    {
        "id": "no_gateway_dns",
        "label": "Console network set to manual with no gateway and no DNS",
        "detail": "Settings > Network > Settings > Set Up Internet Connection > Manual. Give the console a static LAN IP, leave Default Gateway and DNS blank. It can then reach this hub but has no route off the LAN.",
    },
    {
        "id": "router_block",
        "label": "Console blocked from the internet at the router",
        "detail": "A MAC/IP-level WAN block, parental-control rule, or firewall rule. Belt and braces alongside the blank-gateway setting.",
    },
    {
        "id": "auto_update_off",
        "label": "Automatic system and game updates disabled",
        "detail": "Settings > System > System Software > Update Settings, and Settings > Saved Data and Game/App Settings > Automatic Updates.",
    },
    {
        "id": "rest_mode_offline",
        "label": "Rest-mode internet connection turned off",
        "detail": "Settings > System > Power Saving > Features Available in Rest Mode: set 'Stay Connected to the Internet' off.",
    },
    {
        "id": "no_uploads",
        "label": "Automatic capture uploads and data sharing off",
        "detail": "Settings > Captures and Broadcasts, and Settings > Users and Accounts > Privacy / Data collection.",
    },
    {
        "id": "hub_lan_only",
        "label": "This hub is reachable only from the LAN",
        "detail": "No port forward, no reverse proxy, no VPN exit. The subnet allowlist enforces this, but a NAT rule upstream would still be a mistake.",
    },
]


class SafetyPanel:
    """Holds the audit result, probe results, and the persisted checklist."""

    def __init__(self, etahen_client: Any) -> None:
        self.client = etahen_client
        self.path = config.DATA_DIR / "safety_checklist.json"
        self._lock = threading.RLock()
        self._audit: Optional[Dict[str, Any]] = None
        self._probe: Dict[str, Any] = {"last_run": 0.0, "results": [], "ran": False}
        self._checklist: Dict[str, Any] = {}
        self._load_checklist()

    # ------------------------------------------------------------------ #
    # Air-gap audit
    # ------------------------------------------------------------------ #

    def audit_sources(self, force: bool = False) -> Dict[str, Any]:
        """Scan the shipped frontend for any remote reference.

        This is what makes "no CDNs, no remote fonts, no trackers" a checked
        assertion rather than a promise. Findings are graded: a remote URL on a
        line that also looks like a loader (``<script src``, ``@import``,
        ``fetch(``) is blocking; a bare URL in a comment is a note.
        """
        with self._lock:
            if self._audit is not None and not force:
                return self._audit

        allow_patterns = list(ALWAYS_ALLOWED) + [
            str(pattern) for pattern in (config.get("safety.audit_allow_patterns", []) or [])
        ]
        findings: List[Dict[str, Any]] = []
        scanned = 0

        for directory in AUDIT_DIRECTORIES:
            root = config.APP_ROOT / directory
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in AUDIT_EXTENSIONS:
                    continue
                scanned += 1
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    findings.append(
                        {
                            "file": path.relative_to(config.APP_ROOT).as_posix(),
                            "line": 0,
                            "severity": "note",
                            "match": f"unreadable: {exc}",
                        }
                    )
                    continue

                for number, line in enumerate(text.splitlines(), start=1):
                    for match in URL_RE.finditer(line):
                        url = match.group(0)
                        if any(pattern in url for pattern in allow_patterns):
                            continue
                        findings.append(
                            {
                                "file": path.relative_to(config.APP_ROOT).as_posix(),
                                "line": number,
                                "severity": "blocking" if LOADER_HINT_RE.search(line) else "note",
                                "match": url[:160],
                            }
                        )
                    for match in PROTOCOL_RELATIVE_RE.finditer(line):
                        findings.append(
                            {
                                "file": path.relative_to(config.APP_ROOT).as_posix(),
                                "line": number,
                                "severity": "blocking",
                                "match": match.group(0)[:160],
                            }
                        )

        blocking = [f for f in findings if f["severity"] == "blocking"]
        result = {
            "pass": not blocking,
            "files_scanned": scanned,
            "findings": findings,
            "blocking_count": len(blocking),
            "note_count": len(findings) - len(blocking),
            "scope": list(AUDIT_DIRECTORIES),
            "declared_exception": (
                "frame-src is intentionally permissive so the Browser tab can embed "
                "external pages. That is the only outbound path in the UI, and only "
                "for pages you navigate to yourself."
            ),
            "checked_at": time.time(),
        }
        with self._lock:
            self._audit = result
        logger.info(
            "Air-gap audit: %s (%d file(s), %d blocking, %d note)",
            "PASS" if result["pass"] else "FAIL",
            scanned,
            len(blocking),
            result["note_count"],
        )
        return result

    # ------------------------------------------------------------------ #
    # Optional active probing
    # ------------------------------------------------------------------ #

    def probe_psn(self) -> Dict[str, Any]:
        """Resolve + connect-test PSN hostnames from *this PC*.

        Requires ``safety.active_probing``. Two honest caveats, both surfaced in
        the response: this measures the PC's view, not the console's, and running
        it means this machine does make outbound requests.
        """
        if not config.get("safety.active_probing", False):
            return {
                "enabled": False,
                "ran": False,
                "message": "Active probing is disabled. Enable it in Settings to run this check.",
                "results": [],
            }

        timeout = float(config.get("safety.probe_timeout_seconds", 3) or 3)
        results: List[Dict[str, Any]] = []
        for domain in config.get("safety.psn_domains", []) or []:
            entry: Dict[str, Any] = {"domain": str(domain), "resolved": None, "connected": None}
            try:
                addresses = socket.getaddrinfo(str(domain), 443, proto=socket.IPPROTO_TCP)
                entry["resolved"] = sorted({info[4][0] for info in addresses})[:4]
            except OSError as exc:
                entry["resolved"] = []
                entry["resolve_error"] = str(exc)

            if entry["resolved"]:
                sink = {"0.0.0.0", "127.0.0.1", "::"}
                if set(entry["resolved"]) & sink:
                    entry["connected"] = False
                    entry["note"] = "Resolves to a sinkhole address (DNS blocking is active)"
                else:
                    try:
                        with socket.create_connection((entry["resolved"][0], 443), timeout=timeout):
                            entry["connected"] = True
                    except OSError as exc:
                        entry["connected"] = False
                        entry["connect_error"] = str(exc)
            else:
                entry["connected"] = False
            results.append(entry)

        reachable = [item["domain"] for item in results if item.get("connected")]
        payload = {
            "enabled": True,
            "ran": True,
            "last_run": time.time(),
            "results": results,
            "reachable": reachable,
            "scope": "this PC, not the console",
            "caveat": "Running this check makes outbound requests from this machine.",
        }
        with self._lock:
            self._probe = payload
        record_event(
            "safety.probe",
            "PSN probe: {0} of {1} host(s) reachable from this PC".format(len(reachable), len(results)),
            reachable=reachable,
        )
        return payload

    # ------------------------------------------------------------------ #
    # Optional Pi-hole / AdGuard read-out
    # ------------------------------------------------------------------ #

    def pihole_stats(self) -> Dict[str, Any]:
        settings = config.get("safety.pihole", {}) or {}
        if not settings.get("enabled") or not settings.get("base_url"):
            return {"enabled": False}

        base = str(settings["base_url"]).rstrip("/")
        token = str(settings.get("api_token") or "")
        query = {"summaryRaw": ""}
        if token:
            query["auth"] = token
        url = f"{base}/admin/api.php?{urlparse.urlencode(query)}"
        try:
            with urlrequest.urlopen(url, timeout=4) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urlerror.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            return {"enabled": True, "ok": False, "error": str(exc), "base_url": base}

        return {
            "enabled": True,
            "ok": True,
            "base_url": base,
            "blocked_today": payload.get("ads_blocked_today"),
            "queries_today": payload.get("dns_queries_today"),
            "block_percent": payload.get("ads_percentage_today"),
            "domains_on_blocklist": payload.get("domains_being_blocked"),
            "status": payload.get("status"),
        }

    # ------------------------------------------------------------------ #
    # Checklist
    # ------------------------------------------------------------------ #

    def _load_checklist(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(stored, dict):
            self._checklist = stored

    def _flush_checklist(self) -> None:
        try:
            config.write_atomic(self.path, json.dumps(self._checklist, indent=1) + "\n")
        except OSError as exc:
            logger.error("Could not save safety checklist: %s", exc)

    def checklist(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                dict(item, checked=bool((self._checklist.get(item["id"]) or {}).get("checked")),
                     checked_at=(self._checklist.get(item["id"]) or {}).get("at"))
                for item in CHECKLIST_ITEMS
            ]

    def set_checklist_item(self, item_id: str, checked: bool) -> List[Dict[str, Any]]:
        if not any(item["id"] == item_id for item in CHECKLIST_ITEMS):
            raise KeyError(item_id)
        with self._lock:
            self._checklist[item_id] = {"checked": bool(checked), "at": time.time()}
            self._flush_checklist()
        return self.checklist()

    def clear_checklist(self) -> None:
        with self._lock:
            self._checklist = {}
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Assembled state
    # ------------------------------------------------------------------ #

    def quick_level(self) -> str:
        """Cheap level for the 1 Hz dashboard poll.

        Uses only cached data -- no probing, no Pi-hole call -- so the status pip in
        the nav rail costs nothing. The Safety tab calls :meth:`state` for the full
        picture.
        """
        audit = self.audit_sources()
        if not audit["pass"] or not config.get("server.enforce_subnet_allowlist", True):
            return "attention"
        with self._lock:
            probe = dict(self._probe)
        if probe.get("reachable"):
            return "attention"
        checklist = self.checklist()
        all_ticked = all(item["checked"] for item in checklist)
        if probe.get("ran") and not probe.get("reachable") and all_ticked:
            return "checks_pass"
        return "incomplete"

    def state(self) -> Dict[str, Any]:
        """Everything the Safety tab renders, with claims kept separate."""
        audit = self.audit_sources()
        console = self.client.state()
        with self._lock:
            probe = dict(self._probe)
        checklist = self.checklist()
        ticked = sum(1 for item in checklist if item["checked"])

        binding = {
            "bound_ip": config.host_ip(),
            "port": config.port(),
            "base_url": config.base_url(),
            "subnet_allowlist": config.get("server.subnet_allowlist", []),
            "allowlist_enforced": bool(config.get("server.enforce_subnet_allowlist", True)),
        }

        verified: List[str] = []
        unverified: List[str] = []
        warnings: List[str] = []

        verified.append(
            "Hub is bound to {0}:{1} and refuses requests outside {2}.".format(
                binding["bound_ip"], binding["port"], ", ".join(binding["subnet_allowlist"]) or "(none)"
            )
            if binding["allowlist_enforced"]
            else "Hub is bound to {0}:{1}.".format(binding["bound_ip"], binding["port"])
        )
        if not binding["allowlist_enforced"]:
            warnings.append(
                "server.enforce_subnet_allowlist is off - any host that can reach this port is answered."
            )

        if audit["pass"]:
            verified.append(
                "Shipped UI contains no remote script, style, font or image reference "
                "({0} file(s) scanned).".format(audit["files_scanned"])
            )
        else:
            warnings.append(
                "Air-gap audit found {0} blocking remote reference(s) in the shipped UI.".format(
                    audit["blocking_count"]
                )
            )

        if console.get("connected"):
            verified.append(
                "etaHEN on {0} answered a heartbeat {1}s ago.".format(console.get("ip"), console.get("age_seconds"))
            )
        else:
            unverified.append(
                "etaHEN is not currently answering, so console-side state cannot be read."
            )

        if probe.get("ran"):
            if probe.get("reachable"):
                warnings.append(
                    "PSN hosts reachable from this PC: {0}. The PC is not isolated; the console "
                    "may not be either.".format(", ".join(probe["reachable"]))
                )
            else:
                verified.append(
                    "No probed PSN host accepted a connection from this PC at the last check."
                )
        elif config.get("safety.active_probing", False):
            unverified.append("Active probing is enabled but has not been run yet.")
        else:
            unverified.append(
                "PSN reachability has not been probed (active probing is off, which keeps this "
                "machine from making outbound requests)."
            )

        unverified.append(
            "Whether the console itself can reach PSN cannot be determined from here. "
            "The hub talks to etaHEN, not to the console's routing table."
        )

        if ticked < len(checklist):
            unverified.append(
                "{0} of {1} console/router hardening steps are unconfirmed.".format(
                    len(checklist) - ticked, len(checklist)
                )
            )

        if warnings:
            level = "attention"
        elif probe.get("ran") and not probe.get("reachable") and audit["pass"] and ticked == len(checklist):
            level = "checks_pass"
        else:
            level = "incomplete"

        headline = {
            "attention": "Something needs your attention",
            "checks_pass": "All local checks pass",
            "incomplete": "Checks incomplete",
        }[level]

        return {
            "level": level,
            "headline": headline,
            "disclaimer": (
                "This panel reports what can be checked from this PC. It is not, and cannot be, "
                "a guarantee against an account or console ban."
            ),
            "binding": binding,
            "audit": audit,
            "console": console,
            "probe": probe,
            "pihole": self.pihole_stats(),
            "checklist": checklist,
            "checklist_done": ticked,
            "checklist_total": len(checklist),
            "verified": verified,
            "unverified": unverified,
            "warnings": warnings,
            "active_probing": bool(config.get("safety.active_probing", False)),
        }
