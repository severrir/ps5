"""Safety panel: the air-gap audit, the checklist, and how claims are worded.

The wording assertions are not cosmetic. The whole value of this panel is that a
verified fact and an unverifiable hope are never shown as the same thing, so the
separation is tested like any other behaviour.
"""

from __future__ import annotations

from server import config as config_module
from server.safety import CHECKLIST_ITEMS, SafetyPanel


class StubClient:
    def __init__(self, connected=True):
        self.connected = connected

    def state(self):
        return {
            "connected": self.connected,
            "ip": "192.168.1.50",
            "mode": "mock",
            "installer_port": 12800,
            "control_port": 9090,
            "last_ok": 1_700_000_000.0 if self.connected else 0.0,
            "age_seconds": 2.0 if self.connected else None,
            "handshakes": 1,
            "consecutive_failures": 0,
            "last_error": None,
        }


def write_clean_frontend(app_root):
    (app_root / "static").mkdir(parents=True, exist_ok=True)
    (app_root / "templates").mkdir(parents=True, exist_ok=True)
    (app_root / "static" / "app.js").write_text(
        "fetch('/api/system/state').then(function (r) { return r.json(); });\n", encoding="utf-8"
    )
    (app_root / "templates" / "index.html").write_text(
        '<link rel="stylesheet" href="/static/app.css">\n', encoding="utf-8"
    )


# ------------------------------------------------------------------ audit ---- #


def test_clean_frontend_passes_the_audit(sandbox):
    write_clean_frontend(sandbox)
    panel = SafetyPanel(StubClient())

    audit = panel.audit_sources(force=True)

    assert audit["pass"] is True
    assert audit["blocking_count"] == 0
    assert audit["files_scanned"] >= 2


def test_remote_script_is_a_blocking_finding(sandbox):
    write_clean_frontend(sandbox)
    (sandbox / "templates" / "bad.html").write_text(
        '<script src="https://cdn.example.com/jquery.min.js"></script>\n', encoding="utf-8"
    )
    panel = SafetyPanel(StubClient())

    audit = panel.audit_sources(force=True)

    assert audit["pass"] is False
    assert audit["blocking_count"] >= 1
    hits = [f for f in audit["findings"] if f["severity"] == "blocking"]
    assert any("cdn.example.com" in f["match"] for f in hits)
    assert any(f["file"].endswith("bad.html") for f in hits)


def test_remote_font_import_is_blocking(sandbox):
    write_clean_frontend(sandbox)
    (sandbox / "static" / "bad.css").write_text(
        "@import url('https://fonts.googleapis.com/css2?family=Inter');\n", encoding="utf-8"
    )
    panel = SafetyPanel(StubClient())

    audit = panel.audit_sources(force=True)

    assert audit["pass"] is False
    assert any("fonts.googleapis.com" in f["match"] for f in audit["findings"])


def test_protocol_relative_reference_is_blocking(sandbox):
    write_clean_frontend(sandbox)
    (sandbox / "templates" / "rel.html").write_text('<script src="//evil.example/x.js"></script>\n', encoding="utf-8")
    panel = SafetyPanel(StubClient())

    assert panel.audit_sources(force=True)["pass"] is False


def test_url_in_a_comment_is_a_note_not_blocking(sandbox):
    write_clean_frontend(sandbox)
    (sandbox / "static" / "note.js").write_text(
        "/* Spec reference: https://example.org/gamepad-api -- documentation only */\n", encoding="utf-8"
    )
    panel = SafetyPanel(StubClient())

    audit = panel.audit_sources(force=True)

    assert audit["pass"] is True, "a URL in a comment loads nothing and must not fail the audit"
    assert audit["note_count"] >= 1


def test_allow_patterns_are_respected(sandbox):
    write_clean_frontend(sandbox)
    (sandbox / "static" / "allowed.js").write_text(
        'var marker = "https://allowed.invalid/ping"; fetch(marker);\n', encoding="utf-8"
    )
    config_module.save({"safety": {"audit_allow_patterns": ["allowed.invalid"]}})
    panel = SafetyPanel(StubClient())

    assert panel.audit_sources(force=True)["pass"] is True


def test_audit_result_is_cached_until_forced(sandbox):
    write_clean_frontend(sandbox)
    panel = SafetyPanel(StubClient())
    first = panel.audit_sources()

    (sandbox / "static" / "late.js").write_text('fetch("https://late.example/x");\n', encoding="utf-8")

    assert panel.audit_sources()["checked_at"] == first["checked_at"], "cached"
    assert panel.audit_sources(force=True)["pass"] is False, "forced re-run sees the new file"


def test_audit_declares_the_iframe_exception(sandbox):
    write_clean_frontend(sandbox)
    panel = SafetyPanel(StubClient())
    audit = panel.audit_sources(force=True)
    assert "frame-src" in audit["declared_exception"]


# -------------------------------------------------------------- checklist ---- #


def test_checklist_starts_unchecked(sandbox):
    panel = SafetyPanel(StubClient())
    items = panel.checklist()
    assert len(items) == len(CHECKLIST_ITEMS)
    assert all(item["checked"] is False for item in items)


def test_checklist_tick_persists_across_instances(sandbox):
    panel = SafetyPanel(StubClient())
    panel.set_checklist_item("psn_signed_out", True)

    revived = SafetyPanel(StubClient())
    state = {item["id"]: item["checked"] for item in revived.checklist()}
    assert state["psn_signed_out"] is True


def test_unknown_checklist_id_raises(sandbox):
    panel = SafetyPanel(StubClient())
    try:
        panel.set_checklist_item("not-a-step", True)
    except KeyError:
        pass
    else:
        raise AssertionError("an unknown checklist id must raise")


def test_clear_checklist(sandbox):
    panel = SafetyPanel(StubClient())
    panel.set_checklist_item("psn_signed_out", True)
    panel.clear_checklist()
    assert all(item["checked"] is False for item in panel.checklist())


# ------------------------------------------------------------------ state ---- #


def test_state_separates_verified_from_unverified(sandbox):
    write_clean_frontend(sandbox)
    panel = SafetyPanel(StubClient())

    state = panel.state()

    assert isinstance(state["verified"], list) and state["verified"]
    assert isinstance(state["unverified"], list) and state["unverified"]
    # The console's own routing is never claimed as verified.
    assert any("cannot be determined from here" in line for line in state["unverified"])
    assert all("cannot be determined" not in line for line in state["verified"])


def test_state_never_promises_ban_safety(sandbox):
    write_clean_frontend(sandbox)
    panel = SafetyPanel(StubClient())
    state = panel.state()

    assert "not, and cannot be, a guarantee" in state["disclaimer"]
    for line in state["verified"] + state["unverified"]:
        lowered = line.lower()
        assert "ban-safe" not in lowered
        assert "guaranteed" not in lowered
        assert "zero risk" not in lowered


def test_best_level_is_checks_pass_not_safe(sandbox):
    write_clean_frontend(sandbox)
    panel = SafetyPanel(StubClient())
    for item in CHECKLIST_ITEMS:
        panel.set_checklist_item(item["id"], True)
    panel._probe = {"ran": True, "reachable": [], "results": [], "last_run": 1.0}

    state = panel.state()

    assert state["level"] == "checks_pass"
    assert state["headline"] == "All local checks pass"


def test_failed_audit_raises_a_warning_and_drops_the_level(sandbox):
    write_clean_frontend(sandbox)
    (sandbox / "static" / "bad.js").write_text('fetch("https://tracker.example/beacon");\n', encoding="utf-8")
    panel = SafetyPanel(StubClient())

    state = panel.state()

    assert state["level"] == "attention"
    assert any("blocking remote reference" in warning for warning in state["warnings"])


def test_disabled_allowlist_is_a_warning(sandbox):
    write_clean_frontend(sandbox)
    config_module.save({"server": {"enforce_subnet_allowlist": False}})
    panel = SafetyPanel(StubClient())

    state = panel.state()

    assert state["level"] == "attention"
    assert any("enforce_subnet_allowlist is off" in warning for warning in state["warnings"])


def test_reachable_psn_host_is_a_warning(sandbox):
    write_clean_frontend(sandbox)
    panel = SafetyPanel(StubClient())
    panel._probe = {"ran": True, "reachable": ["playstation.net"], "results": [], "last_run": 1.0}

    state = panel.state()

    assert state["level"] == "attention"
    assert any("playstation.net" in warning for warning in state["warnings"])


def test_offline_console_is_unverified_not_a_warning(sandbox):
    write_clean_frontend(sandbox)
    panel = SafetyPanel(StubClient(connected=False))

    state = panel.state()

    assert any("not currently answering" in line for line in state["unverified"])
    assert state["level"] != "attention"


def test_probe_is_refused_while_probing_is_disabled(sandbox):
    panel = SafetyPanel(StubClient())
    result = panel.probe_psn()
    assert result["enabled"] is False
    assert result["ran"] is False
    assert result["results"] == []


def test_pihole_is_reported_as_disabled_by_default(sandbox):
    panel = SafetyPanel(StubClient())
    assert panel.pihole_stats() == {"enabled": False}


def test_quick_level_does_not_probe_or_call_pihole(sandbox):
    write_clean_frontend(sandbox)
    config_module.save({"safety": {"pihole": {"enabled": True, "base_url": "http://127.0.0.1:1"}}})
    panel = SafetyPanel(StubClient())

    # Would hang or error if quick_level touched the network; it must not.
    assert panel.quick_level() in ("attention", "incomplete", "checks_pass")


def test_xml_namespaces_are_exempt_regardless_of_config(sandbox):
    """Regression: namespace URIs were exempted via config, so an existing
    configs.json written before the exemption kept reporting them as findings."""
    write_clean_frontend(sandbox)
    (sandbox / "static" / "sprite.js").write_text(
        'node.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", "#i-x");\n'
        'document.createElementNS("http://www.w3.org/2000/svg", "svg");\n',
        encoding="utf-8",
    )
    # A config that predates the exemption, with the namespaces absent.
    config_module.save({"safety": {"audit_allow_patterns": ["about:blank"]}})

    audit = SafetyPanel(StubClient()).audit_sources(force=True)

    assert audit["pass"] is True
    assert audit["note_count"] == 0, f"unexpected findings: {audit['findings']}"


def test_user_allow_patterns_still_work_alongside_the_builtin_exemptions(sandbox):
    write_clean_frontend(sandbox)
    (sandbox / "static" / "custom.js").write_text('fetch("https://my-nas.local/index.json");\n', encoding="utf-8")
    config_module.save({"safety": {"audit_allow_patterns": ["my-nas.local"]}})

    assert SafetyPanel(StubClient()).audit_sources(force=True)["pass"] is True
