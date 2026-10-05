"""Request gating, response hardening, and PIN hashing.

Three independent concerns, all small enough to live together:

1. **Subnet allowlist.** Every request is checked against
   ``server.subnet_allowlist`` before it reaches a route. The hub exposes package
   installation and a destructive wipe endpoint, so it must never answer a host
   outside your LAN even if the port is accidentally forwarded.
2. **Response headers.** A strict Content-Security-Policy that keeps the UI
   locked to its own origin. ``frame-src`` is the single wide directive, because
   the built-in browser module needs to embed arbitrary pages; this exception is
   declared in the Safety panel rather than hidden.
3. **PIN hashing.** PBKDF2-HMAC-SHA256 with a per-install random salt. A 4-6
   digit PIN has a tiny keyspace, so the iteration count is what buys security
   here; it is configurable and defaults to 240,000.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import ipaddress
import secrets
from typing import Any, Dict, List, Tuple

from flask import Flask, Response, jsonify, request

from . import config
from .logs import logger

# The browser module is the only consumer of a permissive directive. Everything
# else is same-origin. ``img-src data:`` covers the generated placeholder covers,
# which are drawn client-side as data URLs.
CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data: blob:",
    "media-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "frame-src *",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'self'",
)

CSP_HEADER = "; ".join(CSP_DIRECTIVES)


# --------------------------------------------------------------------------- #
# Subnet allowlist
# --------------------------------------------------------------------------- #


def parse_networks(entries: List[str]) -> List[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Turn config strings into network objects, skipping malformed entries."""
    networks: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in entries or []:
        try:
            networks.append(ipaddress.ip_network(str(entry), strict=False))
        except ValueError:
            logger.warning("Ignoring invalid subnet_allowlist entry: %r", entry)
    return networks


def address_allowed(remote: str, entries: List[str]) -> bool:
    """True when ``remote`` falls inside any allowlisted network.

    An unparseable remote address is denied: failing closed is the only safe
    default for an endpoint that can install packages and delete files.
    """
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False

    # IPv6-mapped IPv4 (::ffff:192.168.1.5) should match IPv4 rules.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped

    for network in parse_networks(entries):
        if address.version != network.version:
            continue
        if address in network:
            return True
    return False


def install(app: Flask) -> None:
    """Attach the allowlist gate and the hardening headers to ``app``."""

    @app.before_request
    def _gate_by_subnet():  # type: ignore[misc]
        if not config.get("server.enforce_subnet_allowlist", True):
            return None
        remote = request.remote_addr or ""
        allowlist = config.get("server.subnet_allowlist", []) or []
        if address_allowed(remote, allowlist):
            return None
        logger.warning("Refused request from %s (outside subnet allowlist) %s", remote, request.path)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "forbidden_subnet",
                    "message": "This hub only answers hosts inside server.subnet_allowlist.",
                    "remote": remote,
                }
            ),
            403,
        )

    @app.after_request
    def _harden(response: Response) -> Response:  # type: ignore[misc]
        response.headers["Content-Security-Policy"] = CSP_HEADER
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # The UI polls constantly; never let a proxy or the console cache JSON.
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


# --------------------------------------------------------------------------- #
# PIN hashing
# --------------------------------------------------------------------------- #


def hash_pin(pin: str, iterations: int | None = None, salt_hex: str | None = None) -> Tuple[str, str, int]:
    """Return ``(salt_hex, hash_hex, iterations)`` for ``pin``."""
    rounds = int(iterations or config.get("wipe.pin_iterations", 240000) or 240000)
    rounds = max(50_000, rounds)
    salt = binascii.unhexlify(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, rounds)
    return binascii.hexlify(salt).decode(), binascii.hexlify(digest).decode(), rounds


def verify_pin(pin: str, salt_hex: str, hash_hex: str, iterations: int) -> bool:
    """Constant-time PIN check. False when no PIN has been configured yet."""
    if not salt_hex or not hash_hex:
        return False
    try:
        salt = binascii.unhexlify(salt_hex)
        expected = binascii.unhexlify(hash_hex)
    except (binascii.Error, ValueError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, max(1, int(iterations)))
    return hmac.compare_digest(digest, expected)


def pin_is_configured() -> bool:
    return bool(config.get("wipe.pin_hash")) and bool(config.get("wipe.pin_salt"))


def set_pin(pin: str) -> Dict[str, Any]:
    """Persist a new PIN hash. Returns the stored (non-secret) metadata."""
    salt_hex, hash_hex, rounds = hash_pin(pin)
    config.save({"wipe": {"pin_salt": salt_hex, "pin_hash": hash_hex, "pin_iterations": rounds}})
    logger.info("Emergency wipe PIN set (pbkdf2-sha256, %d iterations)", rounds)
    return {"configured": True, "iterations": rounds}
