"""Request gating, response headers, and PIN hashing."""

from __future__ import annotations

from server import config as config_module, security


# ------------------------------------------------------- subnet allowlist ---- #

LAN = ["192.168.0.0/16", "10.0.0.0/8", "127.0.0.0/8"]


def test_lan_addresses_are_allowed():
    assert security.address_allowed("192.168.1.42", LAN) is True
    assert security.address_allowed("10.3.2.1", LAN) is True
    assert security.address_allowed("127.0.0.1", LAN) is True


def test_public_addresses_are_refused():
    assert security.address_allowed("8.8.8.8", LAN) is False
    assert security.address_allowed("203.0.113.9", LAN) is False
    assert security.address_allowed("172.20.0.5", LAN) is False


def test_ipv6_mapped_ipv4_is_matched_against_ipv4_rules():
    assert security.address_allowed("::ffff:192.168.1.5", LAN) is True
    assert security.address_allowed("::ffff:8.8.8.8", LAN) is False


def test_unparseable_remote_fails_closed():
    assert security.address_allowed("", LAN) is False
    assert security.address_allowed("not-an-ip", LAN) is False
    assert security.address_allowed(None, LAN) is False


def test_empty_allowlist_allows_nothing():
    assert security.address_allowed("192.168.1.1", []) is False


def test_malformed_allowlist_entries_are_skipped_not_fatal():
    assert security.address_allowed("192.168.1.1", ["garbage", "192.168.0.0/16"]) is True


def test_ipv6_rule_matches_ipv6_only():
    rules = ["fd00::/8"]
    assert security.address_allowed("fd00::1", rules) is True
    assert security.address_allowed("192.168.1.1", rules) is False


# ------------------------------------------------------------------- PIN ---- #


def test_pin_hash_verifies_and_rejects(sandbox):
    salt, digest, rounds = security.hash_pin("8421", iterations=60_000)
    assert security.verify_pin("8421", salt, digest, rounds) is True
    assert security.verify_pin("8422", salt, digest, rounds) is False
    assert security.verify_pin("", salt, digest, rounds) is False


def test_pin_salt_is_random_per_call(sandbox):
    first = security.hash_pin("1234", iterations=60_000)
    second = security.hash_pin("1234", iterations=60_000)
    assert first[0] != second[0], "each PIN must get a fresh salt"
    assert first[1] != second[1]


def test_iteration_count_has_a_floor(sandbox):
    _salt, _digest, rounds = security.hash_pin("1234", iterations=10)
    assert rounds >= 50_000, "a trivially low iteration count must be refused"


def test_verify_rejects_missing_or_corrupt_material(sandbox):
    assert security.verify_pin("1234", "", "", 1000) is False
    assert security.verify_pin("1234", "zz", "zz", 1000) is False


def test_set_pin_persists_only_a_hash(sandbox):
    security.set_pin("9182")
    assert security.pin_is_configured() is True
    assert config_module.get("wipe.pin_hash")
    assert config_module.get("wipe.pin_salt")
    # The PIN itself must appear nowhere in the stored config.
    import json

    raw = json.dumps(config_module.raw())
    assert "9182" not in raw


def test_pin_not_configured_by_default(sandbox):
    assert security.pin_is_configured() is False


# ------------------------------------------------- app-level enforcement ---- #


def make_app():
    from server import create_app

    return create_app(start_background=False)


def test_request_from_outside_the_allowlist_is_refused(sandbox):
    app = make_app()
    client = app.test_client()

    response = client.get("/api/ping", environ_base={"REMOTE_ADDR": "203.0.113.7"})

    assert response.status_code == 403
    assert response.get_json()["error"] == "forbidden_subnet"


def test_request_from_the_lan_is_served(sandbox):
    app = make_app()
    client = app.test_client()

    response = client.get("/api/ping", environ_base={"REMOTE_ADDR": "192.168.4.4"})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_allowlist_can_be_disabled(sandbox):
    config_module.save({"server": {"enforce_subnet_allowlist": False}})
    app = make_app()

    response = app.test_client().get("/api/ping", environ_base={"REMOTE_ADDR": "203.0.113.7"})

    assert response.status_code == 200


def test_security_headers_are_present(sandbox):
    app = make_app()
    response = app.test_client().get("/api/ping", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    # The browser module needs this one, and it is the only permissive directive.
    assert "frame-src *" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_api_responses_are_not_cached(sandbox):
    app = make_app()
    response = app.test_client().get("/api/ping", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert response.headers["Cache-Control"] == "no-store"
