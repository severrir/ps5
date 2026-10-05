"""Package parsing: the SFO path, the param.json path, and every fallback."""

from __future__ import annotations

from server import pkg_parser
from tests import synthetic_pkg


def test_sfo_round_trip():
    blob = synthetic_pkg.build_sfo({"TITLE": "Hello World", "APP_VER": "1.07", "ATTRIBUTE": 42})
    parsed = pkg_parser.parse_sfo(blob)
    assert parsed["TITLE"] == "Hello World"
    assert parsed["APP_VER"] == "1.07"
    assert parsed["ATTRIBUTE"] == 42


def test_sfo_rejects_non_sfo_blob():
    assert pkg_parser.parse_sfo(b"not an sfo at all") == {}
    # A correct magic with nonsense behind it must not raise or invent entries.
    assert pkg_parser.parse_sfo(b"\x00PSF" + b"\xff" * 64) == {}


def test_ps4_package_metadata_comes_from_sfo(tmp_path):
    path = synthetic_pkg.build_ps4_pkg(
        tmp_path / "game.pkg",
        content_id="UP0001-CUSA12345_00-0123456789ABCDEF",
        title="Bloodborne Definitive",
        version="1.09",
        category="gd",
    )
    meta = pkg_parser.parse(path)

    assert meta["platform"] == "ps4"
    assert meta["title"] == "Bloodborne Definitive"
    assert meta["title_id"] == "CUSA12345"
    assert meta["content_id"] == "UP0001-CUSA12345_00-0123456789ABCDEF"
    assert meta["version"] == "1.09"
    assert meta["category"] == "Game"
    assert "param.sfo" in meta["metadata_source"]
    assert meta["size_bytes"] > 0
    assert meta["warnings"] == []


def test_patch_category_is_labelled_update(tmp_path):
    path = synthetic_pkg.build_ps4_pkg(tmp_path / "patch.pkg", category="gp", title="Patch 1.10")
    assert pkg_parser.parse(path)["category"] == "Update"


def test_dlc_category_is_labelled(tmp_path):
    path = synthetic_pkg.build_ps4_pkg(tmp_path / "dlc.pkg", category="ac", title="Season Pass")
    assert pkg_parser.parse(path)["category"] == "DLC"


def test_ps5_package_metadata_comes_from_param_json(tmp_path):
    path = synthetic_pkg.build_ps5_pkg(
        tmp_path / "ps5.pkg", title_id="PPSA54321", title="Astro Playroom", version="01.004.000"
    )
    meta = pkg_parser.parse(path)

    assert meta["platform"] == "ps5"
    assert meta["title"] == "Astro Playroom"
    assert meta["title_id"] == "PPSA54321"
    assert meta["version"] == "01.004.000"
    assert "param.json" in meta["metadata_source"]


def test_unknown_container_falls_back_to_filename_and_warns(tmp_path):
    path = synthetic_pkg.build_garbage_pkg(tmp_path / "Some.Weird.Name-v1.02_PS4_FPKG.pkg")
    meta = pkg_parser.parse(path)

    assert meta["platform"] == "unknown"
    assert meta["metadata_source"] == ["filename"]
    assert meta["warnings"], "an unrecognised container should say so"
    assert "Some Weird Name" in meta["title"]


def test_missing_file_returns_a_result_rather_than_raising(tmp_path):
    meta = pkg_parser.parse(tmp_path / "does-not-exist.pkg")
    assert meta["size_bytes"] == 0
    assert meta["warnings"]
    assert meta["title"]


def test_title_from_filename_strips_scene_decoration():
    from pathlib import Path

    cleaned = pkg_parser.title_from_filename(Path("Final.Fantasy.VII.Remake.CUSA17028.v1.02-[FPKG]_EUR.pkg"))
    assert "CUSA17028" not in cleaned
    assert "FPKG" not in cleaned
    assert "Final Fantasy VII Remake" in cleaned


def test_format_size_matches_expected_units():
    assert pkg_parser.format_size(0) == "0 B"
    assert pkg_parser.format_size(512) == "512 B"
    assert pkg_parser.format_size(2048) == "2 KB"
    assert pkg_parser.format_size(5 * 1024 * 1024) == "5.00 MB"
    assert pkg_parser.format_size(int(42.5 * 1024 ** 3)) == "42.50 GB"


def test_detect_magic(tmp_path):
    ps4 = synthetic_pkg.build_ps4_pkg(tmp_path / "a.pkg")
    ps5 = synthetic_pkg.build_ps5_pkg(tmp_path / "b.pkg")
    junk = synthetic_pkg.build_garbage_pkg(tmp_path / "c.pkg")

    assert pkg_parser.detect_magic(ps4) == "ps4"
    assert pkg_parser.detect_magic(ps5) == "ps5"
    assert pkg_parser.detect_magic(junk) == "unknown"


def test_content_id_scan_finds_title_id_without_sfo(tmp_path):
    """A container with no SFO should still yield a title ID from the content ID."""
    path = tmp_path / "noSfo.pkg"
    path.write_bytes(b"\x7fCNT" + b"\x00" * 60 + b"EP4321-CUSA99999_00-ABCDEF0123456789" + b"\x00" * 512)
    meta = pkg_parser.parse(path)
    assert meta["title_id"] == "CUSA99999"
    assert "content_id" in meta["metadata_source"]


def test_title_from_filename_strips_tags_separated_by_underscores():
    """Regression: "_" is a word character, so \bPS4\b never matched inside
    "..._PS4_FPKG" and the tags leaked into the displayed title."""
    from pathlib import Path

    cleaned = pkg_parser.title_from_filename(Path("UNKNOWN.Scene.Release.[2024]-v1.01_PS4_FPKG.pkg"))
    assert cleaned == "UNKNOWN Scene Release"


def test_title_from_filename_keeps_something_when_everything_is_stripped():
    from pathlib import Path

    assert pkg_parser.title_from_filename(Path("CUSA12345_v1.00_PS4.pkg")).strip() != ""
