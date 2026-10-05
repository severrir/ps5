"""HTTP surface: every blueprint exercised through a real test client.

The app is built with ``start_background=False`` so no scanner, worker or watchdog
thread runs; each test drives the services directly where it needs to.
"""

from __future__ import annotations

import json

import pytest

from server import config as config_module, create_app, security
from tests import synthetic_pkg

LAN = {"REMOTE_ADDR": "192.168.1.10"}


@pytest.fixture
def app(sandbox, packages_dir):
    # A real frontend tree, so the air-gap audit has something to scan.
    (sandbox / "static").mkdir(parents=True, exist_ok=True)
    (sandbox / "static" / "app.js").write_text("fetch('/api/system/state');\n", encoding="utf-8")

    synthetic_pkg.build_ps4_pkg(
        packages_dir / "alpha.pkg", title="Alpha Quest", content_id="UP0001-CUSA00001_00-000000000000000A"
    )
    synthetic_pkg.build_ps4_pkg(
        packages_dir / "beta.pkg", title="Beta Chronicles", content_id="UP0001-CUSA00002_00-000000000000000B"
    )

    application = create_app(start_background=False)
    application.extensions["hub"].library.scan()
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def get(client, path):
    return client.get(path, environ_base=LAN)


def post(client, path, body=None):
    return client.post(path, json=body if body is not None else {}, environ_base=LAN)


def delete(client, path):
    return client.delete(path, environ_base=LAN)


# ------------------------------------------------------------------ basics -- #


def test_ping(client):
    response = get(client, "/api/ping")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_state_carries_every_dashboard_section(client):
    payload = get(client, "/api/system/state").get_json()
    for key in ("console", "library", "transfers", "storage", "hardware", "safety_level", "ui", "base_url"):
        assert key in payload, f"the poll payload is missing {key}"


def test_unknown_api_path_returns_json_not_html(client):
    response = get(client, "/api/nope")
    assert response.status_code == 404
    assert response.get_json()["ok"] is False


# ----------------------------------------------------------------- library -- #


def test_library_listing(client):
    payload = get(client, "/api/library").get_json()
    assert payload["count"] == 2
    titles = sorted(item["title"] for item in payload["items"])
    assert titles == ["Alpha Quest", "Beta Chronicles"]
    assert "Game" in payload["categories"]


def test_library_search_and_sort_params(client):
    assert get(client, "/api/library?search=alpha").get_json()["count"] == 1
    assert get(client, "/api/library?search=nothing").get_json()["count"] == 0
    sizes = [item["size_bytes"] for item in get(client, "/api/library?sort=size").get_json()["items"]]
    assert sizes == sorted(sizes, reverse=True)


def test_favorite_toggle_and_rename(client):
    item = get(client, "/api/library").get_json()["items"][0]

    assert post(client, f"/api/library/item/{item['id']}/favorite", {"favorite": True}).get_json()["favorite"] is True
    renamed = post(client, f"/api/library/item/{item['id']}/rename", {"title": "Clean Name"}).get_json()
    assert renamed["title"] == "Clean Name"

    refreshed = get(client, f"/api/library/item/{item['id']}").get_json()["item"]
    assert refreshed["favorite"] is True
    assert refreshed["title"] == "Clean Name"


def test_rename_to_empty_restores_the_parsed_title(client):
    item = get(client, "/api/library").get_json()["items"][0]
    post(client, f"/api/library/item/{item['id']}/rename", {"title": "Temporary"})
    restored = post(client, f"/api/library/item/{item['id']}/rename", {"title": ""}).get_json()
    assert restored["custom"] is False
    assert restored["title"] == item["original_title"]


def test_unknown_item_is_404(client):
    assert get(client, "/api/library/item/deadbeef").status_code == 404
    assert post(client, "/api/library/item/deadbeef/favorite", {"favorite": True}).status_code == 404


def test_hiding_an_item_leaves_the_file_alone(client, packages_dir):
    item = get(client, "/api/library").get_json()["items"][0]
    path = item["path"]

    response = delete(client, f"/api/library/item/{item['id']}")

    assert response.status_code == 200
    assert "untouched" in response.get_json()["message"]
    from pathlib import Path

    assert Path(path).exists()


def test_delete_file_requires_explicit_confirmation(client):
    item = get(client, "/api/library").get_json()["items"][0]

    refused = client.post(
        f"/api/library/item/{item['id']}/delete-file", json={}, environ_base=LAN
    )

    assert refused.status_code == 400
    assert refused.get_json()["error"] == "confirm_required"
    from pathlib import Path

    assert Path(item["path"]).exists()


def test_delete_file_removes_it_when_confirmed(client):
    item = get(client, "/api/library").get_json()["items"][0]

    response = post(client, f"/api/library/item/{item['id']}/delete-file", {"confirm": True})

    assert response.status_code == 200
    from pathlib import Path

    assert not Path(item["path"]).exists()


def test_add_url_validates_the_scheme(client):
    bad = post(client, "/api/library/url", {"url": "not-a-url"})
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "bad_url"

    good = post(client, "/api/library/url", {"url": "http://example.invalid/Some.Game.pkg"})
    assert good.status_code == 200
    assert "Some Game" in good.get_json()["item"]["title"]


def test_rescan_endpoint(client):
    payload = post(client, "/api/library/rescan").get_json()
    assert payload["ok"] is True
    assert payload["items"] == 2


# --------------------------------------------------------------- transfers -- #


def test_enqueue_and_snapshot(client):
    item = get(client, "/api/library").get_json()["items"][0]

    response = post(client, "/api/transfers", {"item_id": item["id"]})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["queued"] == 1
    assert len(payload["queue"]) == 1
    assert payload["queue"][0]["state"] == "queued"


def test_enqueue_batch(client):
    items = get(client, "/api/library").get_json()["items"]
    payload = post(client, "/api/transfers", {"item_ids": [item["id"] for item in items]}).get_json()
    assert payload["queued"] == 2


def test_enqueue_with_no_items_is_a_400(client):
    assert post(client, "/api/transfers", {}).status_code == 400


def test_enqueue_unknown_item_is_a_404(client):
    assert post(client, "/api/transfers", {"item_id": "deadbeef"}).status_code == 404


def test_pause_resume_and_cancel(client):
    item = get(client, "/api/library").get_json()["items"][0]
    transfer = post(client, "/api/transfers", {"item_id": item["id"]}).get_json()["queue"][0]

    assert post(client, f"/api/transfers/{transfer['id']}/pause").get_json()["transfer"]["state"] == "paused"
    assert post(client, f"/api/transfers/{transfer['id']}/resume").get_json()["transfer"]["state"] == "queued"
    assert post(client, f"/api/transfers/{transfer['id']}/cancel").get_json()["transfer"]["state"] == "cancelled"


def test_reorder_and_remove(client):
    items = get(client, "/api/library").get_json()["items"]
    queue = post(client, "/api/transfers", {"item_ids": [item["id"] for item in items]}).get_json()["queue"]
    second = queue[1]["id"]

    moved = post(client, f"/api/transfers/{second}/move", {"delta": -1}).get_json()
    assert moved["queue"][0]["id"] == second

    assert delete(client, f"/api/transfers/{second}").status_code == 200
    assert delete(client, "/api/transfers/nope").status_code == 404


def test_throttle_and_concurrency(client):
    throttle = post(client, "/api/transfers/throttle", {"mbps": 25}).get_json()
    assert throttle["throttle_mbps"] == 25
    assert config_module.get("transfers.throttle_mbps") == 25

    unlimited = post(client, "/api/transfers/throttle", {"mbps": 0}).get_json()
    assert "unlimited" in unlimited["message"]

    assert post(client, "/api/transfers/concurrency", {"value": 3}).get_json()["max_concurrent"] == 3
    assert post(client, "/api/transfers/throttle", {"mbps": "abc"}).status_code == 400


# -------------------------------------------------------- package serving -- #


def test_package_route_streams_the_file(client):
    item = get(client, "/api/library").get_json()["items"][0]

    response = get(client, f"/pkg/{item['id']}")

    assert response.status_code == 200
    assert response.headers["Accept-Ranges"] == "bytes"
    assert len(response.data) == item["size_bytes"]


def test_package_route_supports_range(client):
    item = get(client, "/api/library").get_json()["items"][0]
    response = client.get(f"/pkg/{item['id']}", headers={"Range": "bytes=0-99"}, environ_base=LAN)
    assert response.status_code == 206
    assert len(response.data) == 100


def test_package_route_404s_for_unknown_ids(client):
    assert get(client, "/pkg/deadbeef").status_code == 404


def test_paused_transfer_blocks_the_package_route(client, app):
    item = get(client, "/api/library").get_json()["items"][0]
    transfer = post(client, "/api/transfers", {"item_id": item["id"]}).get_json()["queue"][0]
    post(client, f"/api/transfers/{transfer['id']}/pause")

    response = get(client, f"/pkg/{item['id']}")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"


def test_serving_bytes_feeds_inferred_progress(client, app):
    hub = app.extensions["hub"]
    item = get(client, "/api/library").get_json()["items"][0]
    post(client, "/api/transfers", {"item_id": item["id"]})
    # Mark it active without a console, then serve it.
    hub.transfers._queue[0]["state"] = "installing"

    get(client, f"/pkg/{item['id']}")

    assert hub.transfers.snapshot()["queue"][0]["bytes_served"] == item["size_bytes"]


# ------------------------------------------------------------------ safety -- #


def test_safety_panel_shape(client):
    payload = get(client, "/api/safety").get_json()
    for key in ("level", "headline", "disclaimer", "binding", "audit", "verified", "unverified", "checklist"):
        assert key in payload
    assert "cannot be, a guarantee" in payload["disclaimer"]


def test_audit_endpoint_reports_pass(client):
    payload = post(client, "/api/safety/audit").get_json()
    assert payload["audit"]["pass"] is True


def test_probe_is_refused_until_enabled(client):
    refused = post(client, "/api/safety/probe")
    assert refused.status_code == 409
    assert refused.get_json()["error"] == "probing_disabled"


def test_probing_toggle_explains_the_tradeoff(client):
    payload = post(client, "/api/safety/probing", {"enabled": True}).get_json()
    assert payload["active_probing"] is True
    assert "outbound" in payload["message"]
    post(client, "/api/safety/probing", {"enabled": False})


def test_checklist_endpoint(client):
    payload = post(client, "/api/safety/checklist", {"id": "psn_signed_out", "checked": True}).get_json()
    assert payload["done"] == 1
    assert post(client, "/api/safety/checklist", {"id": "nope", "checked": True}).status_code == 404


# -------------------------------------------------------------------- wipe -- #


def test_wipe_status_before_a_pin_is_set(client):
    payload = get(client, "/api/wipe/status").get_json()
    assert payload["pin_configured"] is False
    assert payload["confirm_phrase"]
    assert "targets" in payload


def test_pin_length_is_validated(client):
    assert post(client, "/api/wipe/pin", {"pin": "12"}).status_code == 400


def test_pin_set_then_change_requires_the_old_one(client):
    assert post(client, "/api/wipe/pin", {"pin": "1234"}).status_code == 200

    wrong = post(client, "/api/wipe/pin", {"pin": "5678", "current_pin": "0000"})
    assert wrong.status_code == 403

    right = post(client, "/api/wipe/pin", {"pin": "5678", "current_pin": "1234"})
    assert right.status_code == 200


def test_plan_requires_credentials(client):
    post(client, "/api/wipe/pin", {"pin": "1234"})

    refused = post(client, "/api/wipe/plan", {"pin": "0000", "phrase": "WIPE EVERYTHING"})
    assert refused.status_code == 403

    accepted = post(client, "/api/wipe/plan", {"pin": "1234", "phrase": "WIPE EVERYTHING"})
    assert accepted.status_code == 200
    assert accepted.get_json()["token"]
    assert "Nothing has been deleted" in accepted.get_json()["note"]


def test_execute_without_a_plan_is_refused(client):
    post(client, "/api/wipe/pin", {"pin": "1234"})
    response = post(client, "/api/wipe/execute", {"token": "fabricated"})
    assert response.status_code == 409
    assert response.get_json()["error"] == "unknown_token"


def test_execute_with_a_real_plan_does_not_stop_the_server_when_asked(client, sandbox):
    logs = sandbox / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "session-x.log").write_text("data", encoding="utf-8")
    config_module.save({"wipe": {"allowlist": [str(logs)]}})

    post(client, "/api/wipe/pin", {"pin": "1234"})
    plan = post(client, "/api/wipe/plan", {"pin": "1234", "phrase": "WIPE EVERYTHING"}).get_json()

    report = post(client, "/api/wipe/execute", {"token": plan["token"], "stop_server": False}).get_json()

    assert report["ok"] is True
    assert report["server_stopping"] is False
    assert not (logs / "session-x.log").exists()


def test_safe_mode_guide_endpoint(client):
    guide = get(client, "/api/wipe/safe-mode").get_json()["guide"]
    assert len(guide["options"]) == 7


# ---------------------------------------------------------------- settings -- #


def test_settings_never_expose_pin_material(client):
    post(client, "/api/wipe/pin", {"pin": "1234"})
    payload = get(client, "/api/system/settings").get_json()
    assert "wipe" not in payload["settings"]
    assert "pin_hash" not in json.dumps(payload)


def test_settings_write_allowlist(client):
    ok = post(client, "/api/system/settings", {"ui.cards_per_row": 6})
    assert ok.status_code == 200
    assert config_module.get("ui.cards_per_row") == 6

    refused = post(client, "/api/system/settings", {"wipe.confirm_phrase": "EASY"})
    assert refused.status_code == 403
    assert refused.get_json()["rejected"] == ["wipe.confirm_phrase"]
    assert config_module.get("wipe.confirm_phrase") == "WIPE EVERYTHING"


def test_settings_reject_unknown_keys(client):
    response = post(client, "/api/system/settings", {"server.port": 9999})
    assert response.status_code == 403
    assert config_module.get("server.port") == 8080


def test_empty_settings_body_is_a_400(client):
    assert post(client, "/api/system/settings", {}).status_code == 400


# ------------------------------------------------------------------ system -- #


def test_logs_endpoint(client):
    payload = get(client, "/api/system/logs").get_json()
    assert "lines" in payload
    assert "last_seq" in payload


def test_events_endpoint_records_actions(client):
    item = get(client, "/api/library").get_json()["items"][0]
    post(client, "/api/transfers", {"item_id": item["id"]})

    events = get(client, "/api/system/events").get_json()["events"]

    assert any(event["kind"] == "transfer.queued" for event in events)


def test_storage_endpoint_includes_the_host(client):
    payload = get(client, "/api/system/storage").get_json()
    assert payload["ok"] is True
    assert isinstance(payload["host"], list)


def test_bgm_listing_is_empty_but_explains_itself(client):
    payload = get(client, "/api/system/bgm").get_json()
    assert payload["tracks"] == []
    assert "bgm/" in payload["hint"] or "bgm" in payload["folder"]


def test_version_check_is_local_only(client):
    payload = get(client, "/api/system/version").get_json()
    assert payload["update_available"] is False
    assert "never contacts a remote" in payload["note"]


def test_staged_update_is_detected(client, sandbox):
    manifest = sandbox / "update" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"version": "99.0.0", "notes": "test"}), encoding="utf-8")

    payload = get(client, "/api/system/version").get_json()

    assert payload["update_available"] is True
    assert payload["staged"]["version"] == "99.0.0"


def test_backup_without_sources_explains_what_to_configure(client):
    response = post(client, "/api/system/backup")
    assert response.status_code == 400
    assert response.get_json()["error"] == "no_sources"
    assert "backup.source_dirs" in response.get_json()["message"]


def test_backup_bundles_configured_folders(client, sandbox):
    saves = sandbox / "saves"
    saves.mkdir()
    (saves / "slot1.bin").write_bytes(b"\x01" * 256)
    config_module.save({"backup": {"source_dirs": [str(saves)]}})

    payload = post(client, "/api/system/backup").get_json()

    assert payload["ok"] is True
    assert payload["files"] == 1
    from pathlib import Path

    assert Path(payload["archive"]).exists()


# ----------------------------------------------------------------- browser -- #


def test_browser_history_round_trip(client):
    assert get(client, "/api/browser/history").get_json()["history"] == []

    post(client, "/api/browser/history", {"url": "https://example.invalid/a", "title": "A", "framed": True})
    post(client, "/api/browser/history", {"url": "https://example.invalid/b", "title": "B", "framed": False})

    history = get(client, "/api/browser/history").get_json()["history"]
    assert [entry["title"] for entry in history] == ["B", "A"]

    assert delete(client, "/api/browser/history").status_code == 200
    assert get(client, "/api/browser/history").get_json()["history"] == []


def test_browser_history_rejects_an_empty_url(client):
    assert post(client, "/api/browser/history", {"url": ""}).status_code == 400


def test_repeat_visits_collapse(client):
    post(client, "/api/browser/history", {"url": "https://example.invalid/a", "title": "A"})
    post(client, "/api/browser/history", {"url": "https://example.invalid/a", "title": "A again"})
    history = get(client, "/api/browser/history").get_json()["history"]
    assert len(history) == 1
    assert history[0]["title"] == "A again"


def test_bookmarks_round_trip(client):
    payload = post(
        client, "/api/browser/bookmarks", {"bookmarks": [{"title": "Docs", "url": "https://example.invalid/docs"}]}
    ).get_json()
    assert len(payload["bookmarks"]) == 1

    assert get(client, "/api/browser/bookmarks").get_json()["bookmarks"][0]["title"] == "Docs"


def test_bookmarks_reject_a_non_list(client):
    assert post(client, "/api/browser/bookmarks", {"bookmarks": "nope"}).status_code == 400


def test_added_link_is_immediately_queueable(client):
    """Regression: a pasted link had to wait for the next background scan.

    The UI adds a link and then queues it in the same gesture, so the item must be
    in the live index the moment the POST returns.
    """
    added = post(client, "/api/library/url", {"url": "http://example.invalid/Instant.Game.pkg"}).get_json()

    assert get(client, f"/api/library/item/{added['item']['id']}").status_code == 200
    queued = post(client, "/api/transfers", {"item_id": added["item"]["id"]})
    assert queued.status_code == 200
    assert queued.get_json()["queued"] == 1


def test_pin_is_visible_immediately(client):
    """Regression: overrides were only applied on the next scan."""
    item = get(client, "/api/library").get_json()["items"][0]
    post(client, f"/api/library/item/{item['id']}/favorite", {"favorite": True})

    listing = get(client, "/api/library").get_json()["items"]
    assert listing[0]["id"] == item["id"], "a pinned item must lead the grid at once"
    assert listing[0]["favorite"] is True
