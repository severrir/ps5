"""Configuration: schema parity, merge rules, token expansion, atomic save."""

from __future__ import annotations

import json
from pathlib import Path

from server import config as config_module

REAL_ROOT = Path(__file__).resolve().parent.parent


def key_paths(node, prefix=""):
    """Every dotted key path in a nested dict, ignoring documentation keys."""
    paths = set()
    for key, value in node.items():
        if key.startswith("_"):
            continue
        path = prefix + key
        paths.add(path)
        if isinstance(value, dict):
            paths |= key_paths(value, path + ".")
    return paths


def test_example_config_matches_defaults():
    """configs.example.json documents DEFAULTS; neither may drift from the other."""
    with open(REAL_ROOT / "configs.example.json", "r", encoding="utf-8") as handle:
        example = json.load(handle)

    expected = key_paths(config_module.DEFAULTS)
    actual = key_paths(example)

    missing = expected - actual
    extra = actual - expected
    assert not missing, f"configs.example.json is missing: {sorted(missing)}"
    assert not extra, f"configs.example.json has keys not in DEFAULTS: {sorted(extra)}"


def test_example_config_is_valid_json():
    with open(REAL_ROOT / "configs.example.json", "r", encoding="utf-8") as handle:
        assert isinstance(json.load(handle), dict)


def test_deep_merge_replaces_lists_but_merges_dicts():
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2, 3]}
    override = {"a": {"c": 9}, "list": [7]}
    merged = config_module.deep_merge(base, override)

    assert merged == {"a": {"b": 1, "c": 9}, "list": [7]}
    # The original must be untouched.
    assert base["a"]["c"] == 2


def test_expand_tokens_walks_nested_structures():
    expanded = config_module.expand_tokens(
        {"path": "{app}/x", "list": ["{app}/y"], "deep": {"p": "{app}"}, "number": 5}
    )
    root = config_module.APP_ROOT.as_posix()
    assert expanded["path"] == root + "/x"
    assert expanded["list"] == [root + "/y"]
    assert expanded["deep"]["p"] == root
    assert expanded["number"] == 5


def test_first_run_creates_config(sandbox):
    assert (sandbox / "configs.json").exists(), "load() should materialise a config on first run"


def test_save_merges_and_reloads(sandbox):
    config_module.save({"transfers": {"max_concurrent": 3}})
    assert config_module.get("transfers.max_concurrent") == 3
    # Untouched siblings survive.
    assert config_module.get("transfers.max_retries") == 3

    with open(sandbox / "configs.json", "r", encoding="utf-8") as handle:
        on_disk = json.load(handle)
    assert on_disk["transfers"]["max_concurrent"] == 3


def test_broken_config_is_quarantined_not_fatal(sandbox):
    (sandbox / "configs.json").write_text("{ this is not json", encoding="utf-8")
    config_module.reset_cache()

    loaded = config_module.load(force_reload=True)

    assert loaded["server"]["port"] == 8080, "defaults should take over"
    assert (sandbox / "configs.json.broken").exists(), "the bad file should be kept for inspection"


def test_get_returns_default_for_missing_path(sandbox):
    assert config_module.get("nope.not.here", "fallback") == "fallback"


def test_base_url_uses_resolved_host(sandbox):
    config_module.save({"server": {"host_ip": "192.168.5.5", "port": 9999}})
    assert config_module.base_url() == "http://192.168.5.5:9999"


def test_pkg_directories_skips_missing(sandbox):
    present = sandbox / "present"
    present.mkdir()
    config_module.save({"library": {"pkg_directories": [str(present), str(sandbox / "absent")]}})
    found = [str(path) for path in config_module.pkg_directories()]
    assert found == [str(present)]


def test_detect_lan_ip_returns_an_address():
    address = config_module.detect_lan_ip()
    assert address.count(".") == 3 or ":" in address


def test_runtime_binding_overrides_the_configured_port(sandbox):
    """Regression: --port changed where the server listened but not the URL the
    console was told to fetch from, so every install 404'd on a non-default port."""
    config_module.save({"server": {"host_ip": "192.168.5.5", "port": 8080}})
    assert config_module.base_url() == "http://192.168.5.5:8080"

    config_module.set_runtime_binding(port=8088)

    assert config_module.port() == 8088
    assert config_module.base_url() == "http://192.168.5.5:8088"


def test_runtime_binding_overrides_the_host(sandbox):
    config_module.set_runtime_binding(host="10.1.1.7", port=9000)
    assert config_module.base_url() == "http://10.1.1.7:9000"


def test_wildcard_bind_is_not_advertised_to_the_console(sandbox):
    """0.0.0.0 is bindable but not connectable; the LAN address must be kept."""
    config_module.save({"server": {"host_ip": "192.168.5.5"}})
    config_module.set_runtime_binding(host="0.0.0.0", port=8080)
    assert config_module.host_ip() == "192.168.5.5"
    assert "0.0.0.0" not in config_module.base_url()


def test_runtime_binding_is_not_persisted(sandbox):
    config_module.set_runtime_binding(port=1234)
    assert config_module.port() == 1234
    assert config_module.raw()["server"]["port"] == 8080, "an override is for one run only"
