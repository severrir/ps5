"""Emergency wipe: refusal rules, authentication, lockout, dry run, execution.

These are the tests that matter most in the project. A wipe that deletes the wrong
path is unrecoverable, so the refusal rules are checked individually and the
execution path is checked to touch nothing that was not in the plan.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from server import config as config_module, security
from server.wipe import SAFE_MODE_GUIDE, WipeGuard

PHRASE = "WIPE EVERYTHING"
PIN = "4921"


@pytest.fixture
def guard(sandbox):
    security.set_pin(PIN)
    return WipeGuard()


def seed_targets(sandbox):
    """Create a realistic set of wipe targets and point the allowlist at them."""
    logs = sandbox / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "session-20260101.log").write_text("x" * 500, encoding="utf-8")
    (logs / "session-20260102.log").write_text("y" * 700, encoding="utf-8")

    data = sandbox / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "transfers.json").write_text('{"queue": []}', encoding="utf-8")
    (data / "browser_history.json").write_text("[]", encoding="utf-8")

    config_module.save(
        {
            "wipe": {
                "allowlist": [
                    str(logs),
                    str(data / "transfers.json"),
                    str(data / "browser_history.json"),
                    str(data / "never-created.json"),
                ]
            }
        }
    )
    return logs, data


# --------------------------------------------------------- refusal rules ---- #


def test_filesystem_root_is_refused(guard, sandbox):
    reason = guard._refusal_reason(Path(Path(sandbox).anchor))
    assert reason is not None
    assert "root" in reason


def test_app_folder_itself_is_refused(guard, sandbox):
    reason = guard._refusal_reason(sandbox)
    assert reason is not None
    assert "own folder" in reason


def test_ancestor_of_the_app_is_refused(guard, sandbox):
    reason = guard._refusal_reason(sandbox.parent)
    assert reason is not None
    assert "ancestor" in reason


def test_the_hubs_own_source_is_refused(guard, sandbox):
    for protected in ("server", "static", "templates", "run.py"):
        reason = guard._refusal_reason(sandbox / protected)
        assert reason is not None, f"{protected} must be protected"
        assert "own source" in reason


def test_a_file_inside_the_source_tree_is_refused(guard, sandbox):
    target = sandbox / "server" / "wipe.py"
    target.write_text("# code\n", encoding="utf-8")
    reason = guard._refusal_reason(target)
    assert reason is not None
    assert "own source" in reason


def test_home_folder_is_refused(guard):
    """Either rule may catch it: pytest's tmp_path lives under the home folder, so
    the ancestor check usually fires first. What matters is that it is refused."""
    reason = guard._refusal_reason(Path.home())
    assert reason is not None
    assert "home folder" in reason or "ancestor" in reason


def test_home_folder_rule_fires_when_it_is_not_an_ancestor(guard, monkeypatch, tmp_path):
    """Exercise the home-folder rule itself, with the app rooted outside home."""
    outside = Path(tmp_path.anchor) / "elsewhere" / "hub"
    monkeypatch.setattr(config_module, "APP_ROOT", outside)
    monkeypatch.setattr(config_module, "PROTECTED_PATHS", (outside / "server", outside / "run.py"))
    reason = guard._refusal_reason(Path.home())
    assert reason is not None
    assert "home folder" in reason


def test_system_directories_are_refused(guard):
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    reason = guard._refusal_reason(windows)
    assert reason is not None
    assert "system directory" in reason


def test_ordinary_path_is_accepted(guard, sandbox):
    target = sandbox / "logs"
    target.mkdir(parents=True, exist_ok=True)
    assert guard._refusal_reason(target) is None


def test_symlink_is_refused(guard, sandbox):
    real = sandbox / "real-dir"
    real.mkdir()
    link = sandbox / "link-dir"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs privileges on this machine")
    reason = guard._refusal_reason(link)
    assert reason is not None
    assert "symlink" in reason


# ---------------------------------------------------------------- status ---- #


def test_status_splits_actionable_missing_and_refused(guard, sandbox):
    logs, data = seed_targets(sandbox)
    config_module.save({"wipe": {"allowlist": config_module.get("wipe.allowlist") + [str(sandbox / "server")]}})

    status = guard.status()
    targets = status["targets"]

    actionable = [item["path"] for item in targets["actionable"]]
    assert str(logs) in actionable
    assert str(data / "transfers.json") in actionable
    assert [item["path"] for item in targets["missing"]] == [str(data / "never-created.json")]
    assert [item["path"] for item in targets["refused"]] == [str(sandbox / "server")]
    assert status["total_bytes"] > 0


def test_status_reports_console_paths_as_skipped_without_an_endpoint(guard, sandbox):
    config_module.save({"wipe": {"console_paths": ["/data/etaHEN/cache"]}})
    endpoints = dict(config_module.get("etahen.endpoints"))
    endpoints.pop("delete_path", None)
    config_module.save({"etahen": {"endpoints": endpoints}})

    console = guard.status()["targets"]["console"]

    assert len(console) == 1
    assert console[0]["supported"] is False
    assert "delete_path" in console[0]["reason"]


def test_status_lists_protected_paths(guard, sandbox):
    protected = guard.status()["protected"]
    assert any(path.endswith("server") for path in protected)
    assert any(path.endswith("run.py") for path in protected)


# -------------------------------------------------------- authentication ---- #


def test_correct_credentials_authenticate(guard, sandbox):
    ok, detail = guard.authenticate(PIN, PHRASE)
    assert ok is True
    assert detail == {}


def test_wrong_pin_is_refused(guard, sandbox):
    ok, detail = guard.authenticate("0000", PHRASE)
    assert ok is False
    assert detail["error"] == "bad_credentials"


def test_wrong_phrase_is_refused_even_with_the_right_pin(guard, sandbox):
    ok, detail = guard.authenticate(PIN, "wipe everything")
    assert ok is False
    assert detail["error"] == "bad_credentials"


def test_failure_message_does_not_say_which_factor_was_wrong(guard, sandbox):
    _ok, from_pin = guard.authenticate("0000", PHRASE)
    _ok2, from_phrase = guard.authenticate(PIN, "nope")
    assert from_pin["message"] == from_phrase["message"]


def test_lockout_after_the_configured_attempts(sandbox):
    security.set_pin(PIN)
    config_module.save({"wipe": {"max_attempts": 3, "lockout_minutes": 15}})
    guard = WipeGuard()

    for _ in range(2):
        ok, detail = guard.authenticate("0000", PHRASE)
        assert ok is False
        assert detail["locked"] is False

    ok, detail = guard.authenticate("0000", PHRASE)
    assert detail["locked"] is True
    assert guard.locked_for() > 0

    # Even the right credentials are refused while locked out.
    ok, detail = guard.authenticate(PIN, PHRASE)
    assert ok is False
    assert detail["error"] == "locked_out"


def test_lockout_survives_a_restart(sandbox):
    security.set_pin(PIN)
    config_module.save({"wipe": {"max_attempts": 1, "lockout_minutes": 15}})
    guard = WipeGuard()
    guard.authenticate("0000", PHRASE)
    assert guard.locked_for() > 0

    revived = WipeGuard()
    assert revived.locked_for() > 0, "restarting the hub must not clear a lockout"


def test_successful_auth_clears_the_attempt_counter(sandbox):
    security.set_pin(PIN)
    config_module.save({"wipe": {"max_attempts": 5}})
    guard = WipeGuard()
    guard.authenticate("0000", PHRASE)
    guard.authenticate(PIN, PHRASE)
    assert guard.status()["attempts_used"] == 0


def test_wipe_without_a_pin_is_refused(sandbox):
    guard = WipeGuard()
    ok, detail = guard.authenticate("", PHRASE)
    assert ok is False
    assert detail["error"] == "no_pin"


# ---------------------------------------------------------------- plan ------ #


def test_plan_requires_credentials_and_deletes_nothing(guard, sandbox):
    logs, _data = seed_targets(sandbox)

    bad = guard.plan("0000", PHRASE)
    assert bad["ok"] is False
    assert (logs / "session-20260101.log").exists()

    good = guard.plan(PIN, PHRASE)
    assert good["ok"] is True
    assert good["token"]
    assert good["total_bytes"] > 0
    assert (logs / "session-20260101.log").exists(), "a dry run must not delete anything"


def test_plan_lists_exactly_what_would_be_deleted(guard, sandbox):
    logs, data = seed_targets(sandbox)
    plan = guard.plan(PIN, PHRASE)

    paths = [item["path"] for item in plan["targets"]["actionable"]]
    assert str(logs) in paths
    assert str(data / "transfers.json") in paths
    log_entry = next(item for item in plan["targets"]["actionable"] if item["path"] == str(logs))
    assert log_entry["file_count"] == 2


def test_only_one_plan_token_is_live_at_a_time(guard, sandbox):
    seed_targets(sandbox)
    first = guard.plan(PIN, PHRASE)["token"]
    second = guard.plan(PIN, PHRASE)["token"]

    stale = guard.execute(first, stop_server=False)
    assert stale["ok"] is False
    assert stale["error"] == "unknown_token"

    assert guard.execute(second, stop_server=False)["ok"] is True


# ------------------------------------------------------------- execute ------ #


def test_execute_requires_a_token(guard, sandbox):
    seed_targets(sandbox)
    result = guard.execute("made-up-token", stop_server=False)
    assert result["ok"] is False
    assert result["error"] == "unknown_token"


def test_expired_token_is_refused(guard, sandbox):
    logs, _data = seed_targets(sandbox)
    config_module.save({"wipe": {"plan_ttl_seconds": 15}})
    plan = guard.plan(PIN, PHRASE)
    guard._plans[plan["token"]]["expires_at"] = time.time() - 1

    result = guard.execute(plan["token"], stop_server=False)

    assert result["ok"] is False
    assert result["error"] == "expired_token"
    assert (logs / "session-20260101.log").exists()


def test_execute_deletes_the_planned_targets(guard, sandbox):
    logs, data = seed_targets(sandbox)
    plan = guard.plan(PIN, PHRASE)

    report = guard.execute(plan["token"], stop_server=False)

    assert report["ok"] is True
    assert report["removed_files"] >= 3
    assert report["freed_bytes"] > 0
    assert not (logs / "session-20260101.log").exists()
    assert not (data / "transfers.json").exists()
    # Directories survive so logging can continue and the allowlist stays valid.
    assert logs.is_dir()


def test_execute_leaves_the_hubs_source_alone(guard, sandbox):
    seed_targets(sandbox)
    (sandbox / "server" / "wipe.py").write_text("# important\n", encoding="utf-8")
    (sandbox / "static").mkdir(exist_ok=True)
    (sandbox / "static" / "app.js").write_text("// important\n", encoding="utf-8")

    plan = guard.plan(PIN, PHRASE)
    guard.execute(plan["token"], stop_server=False)

    assert (sandbox / "server" / "wipe.py").exists()
    assert (sandbox / "static" / "app.js").exists()
    assert (sandbox / "run.py").exists()


def test_execute_does_not_touch_packages(guard, sandbox):
    seed_targets(sandbox)
    packages = sandbox / "packages"
    packages.mkdir(exist_ok=True)
    keeper = packages / "game.pkg"
    keeper.write_bytes(b"\x7fCNT" + b"\x00" * 100)

    plan = guard.plan(PIN, PHRASE)
    guard.execute(plan["token"], stop_server=False)

    assert keeper.exists(), "packages are never in the default allowlist"


def test_execute_runs_the_cleanup_hook(sandbox):
    security.set_pin(PIN)
    seed_targets(sandbox)
    called = {"count": 0}

    def hook():
        called["count"] += 1

    guard = WipeGuard(on_wiped=hook)
    plan = guard.plan(PIN, PHRASE)
    guard.execute(plan["token"], stop_server=False)

    assert called["count"] == 1


def test_a_failing_cleanup_hook_does_not_mask_the_wipe(sandbox):
    security.set_pin(PIN)
    seed_targets(sandbox)

    def hook():
        raise RuntimeError("cleanup exploded")

    guard = WipeGuard(on_wiped=hook)
    plan = guard.plan(PIN, PHRASE)
    report = guard.execute(plan["token"], stop_server=False)

    assert report["ok"] is True


def test_empty_allowlist_wipes_nothing(guard, sandbox):
    config_module.save({"wipe": {"allowlist": []}})
    plan = guard.plan(PIN, PHRASE)
    report = guard.execute(plan["token"], stop_server=False)

    assert report["ok"] is True
    assert report["removed_files"] == 0
    assert report["freed_bytes"] == 0


def test_measure_counts_a_directory_tree(guard, sandbox):
    tree = sandbox / "tree"
    (tree / "deep").mkdir(parents=True)
    (tree / "a.txt").write_text("1234", encoding="utf-8")
    (tree / "deep" / "b.txt").write_text("567890", encoding="utf-8")

    count, size = guard._measure(tree)

    assert count == 2
    assert size == 10


# --------------------------------------------------------- safe mode guide -- #


def test_safe_mode_guide_is_complete():
    assert SAFE_MODE_GUIDE["title"]
    assert len(SAFE_MODE_GUIDE["entering"]) >= 3
    assert len(SAFE_MODE_GUIDE["options"]) == 7, "the console offers exactly seven Safe Mode options"
    numbers = [option["n"] for option in SAFE_MODE_GUIDE["options"]]
    assert numbers == [1, 2, 3, 4, 5, 6, 7]
    assert len(SAFE_MODE_GUIDE["usb_reinstall"]) >= 4
    assert SAFE_MODE_GUIDE["cautions"]


def test_safe_mode_guide_warns_about_the_destructive_options():
    text = " ".join(SAFE_MODE_GUIDE["cautions"]).lower()
    assert "irreversible" in text
    option_seven = SAFE_MODE_GUIDE["options"][6]
    assert "reinstall" in option_seven["name"].lower()


def test_safe_mode_guide_mentions_the_pup_path():
    usb_text = " ".join(SAFE_MODE_GUIDE["usb_reinstall"])
    assert "PS5\\UPDATE" in usb_text
    assert "PS5UPDATE.PUP" in usb_text
