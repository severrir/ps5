"""Library: scanning, stable ids, user overrides, filtering, sorting."""

from __future__ import annotations

from server import config as config_module
from server.library import Library, LibraryStore, _item_id
from tests import synthetic_pkg


def build_library(packages_dir):
    synthetic_pkg.build_ps4_pkg(packages_dir / "alpha.pkg", title="Alpha Quest", content_id="UP0001-CUSA00001_00-000000000000000A")
    synthetic_pkg.build_ps4_pkg(
        packages_dir / "zulu.pkg", title="Zulu Racing", content_id="UP0001-CUSA00002_00-000000000000000B", padding=40000
    )
    synthetic_pkg.build_ps5_pkg(packages_dir / "mid.pkg", title="Mid Adventure", title_id="PPSA00003")
    library = Library()
    library.scan()
    return library


def test_scan_finds_every_package(packages_dir):
    library = build_library(packages_dir)
    titles = sorted(item["title"] for item in library.all_items())
    assert titles == ["Alpha Quest", "Mid Adventure", "Zulu Racing"]


def test_item_ids_are_stable_across_scans(packages_dir):
    library = build_library(packages_dir)
    first = sorted(item["id"] for item in library.all_items())
    library.scan()
    assert sorted(item["id"] for item in library.all_items()) == first


def test_nested_directories_are_scanned(packages_dir):
    synthetic_pkg.build_ps4_pkg(packages_dir / "sub" / "deep" / "nested.pkg", title="Nested Title")
    library = Library()
    library.scan()
    assert any(item["title"] == "Nested Title" for item in library.all_items())


def test_non_pkg_files_are_ignored(packages_dir):
    (packages_dir / "readme.txt").write_text("not a package", encoding="utf-8")
    library = build_library(packages_dir)
    assert all(item["filename"].endswith(".pkg") for item in library.all_items())


def test_rename_override_survives_rescan(packages_dir):
    library = build_library(packages_dir)
    target = next(item for item in library.all_items() if item["title"] == "Alpha Quest")

    library.store.set_override(target["id"], custom_title="My Clean Name")
    library.scan()

    renamed = library.item(target["id"])
    assert renamed["title"] == "My Clean Name"
    assert renamed["original_title"] == "Alpha Quest", "the parsed title must still be available"


def test_favorites_lead_the_grid_regardless_of_sort(packages_dir):
    library = build_library(packages_dir)
    zulu = next(item for item in library.all_items() if item["title"] == "Zulu Racing")
    library.store.set_override(zulu["id"], favorite=True)
    library.scan()

    ordered = library.query(sort="title")
    assert ordered[0]["title"] == "Zulu Racing"


def test_hidden_items_are_excluded_unless_requested(packages_dir):
    library = build_library(packages_dir)
    target = library.all_items()[0]
    library.store.set_override(target["id"], hidden=True)
    library.scan()

    assert len(library.all_items()) == 2
    assert len(library.all_items(include_hidden=True)) == 3


def test_search_matches_title_and_id(packages_dir):
    library = build_library(packages_dir)
    assert len(library.query(search="zulu")) == 1
    assert len(library.query(search="CUSA00001")) == 1
    assert len(library.query(search="nothing here")) == 0
    # All terms must match, not any.
    assert len(library.query(search="alpha racing")) == 0


def test_sort_by_size_is_descending(packages_dir):
    library = build_library(packages_dir)
    sizes = [item["size_bytes"] for item in library.query(sort="size")]
    assert sizes == sorted(sizes, reverse=True)


def test_url_entries_appear_as_items(packages_dir):
    library = build_library(packages_dir)
    entry = library.store.add_url("http://example.invalid/some/Great.Game.pkg")
    library.scan()

    item = library.item(entry["id"])
    assert item is not None
    assert item["source"] == "url"
    assert item["url"].endswith("Great.Game.pkg")
    assert "Great Game" in item["title"]


def test_adding_the_same_url_twice_is_idempotent(packages_dir):
    store = LibraryStore()
    first = store.add_url("http://example.invalid/a.pkg")
    second = store.add_url("http://example.invalid/a.pkg")
    assert first["id"] == second["id"]
    assert len(store.urls()) == 1


def test_remove_url(packages_dir):
    store = LibraryStore()
    entry = store.add_url("http://example.invalid/b.pkg")
    assert store.remove_url(entry["id"]) is True
    assert store.urls() == []


def test_cover_is_used_when_the_file_exists(packages_dir):
    config_module.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    (config_module.COVERS_DIR / "CUSA00001.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    library = build_library(packages_dir)
    item = next(item for item in library.all_items() if item["title_id"] == "CUSA00001")
    assert item["cover"] == "/covers/CUSA00001.png"

    other = next(item for item in library.all_items() if item["title_id"] == "CUSA00002")
    assert other["cover"] is None, "a missing cover must stay None so the UI draws a placeholder"


def test_cover_lookup_rejects_path_traversal():
    assert Library.cover_for("../../secret") is None
    assert Library.cover_for("") is None
    assert Library.cover_for(None) is None


def test_status_reports_totals(packages_dir):
    library = build_library(packages_dir)
    status = library.status()
    assert status["items"] == 3
    assert status["total_bytes"] > 0
    assert status["last_scan"] > 0
    assert status["error"] is None


def test_max_items_limit_is_respected(packages_dir):
    for index in range(6):
        synthetic_pkg.build_ps4_pkg(packages_dir / f"many{index}.pkg", title=f"Game {index}")
    config_module.save({"library": {"max_items": 3}})

    library = Library()
    library.scan()
    assert len(library.all_items()) <= 3


def test_store_clear_forgets_everything(packages_dir):
    library = build_library(packages_dir)
    target = library.all_items()[0]
    library.store.set_override(target["id"], favorite=True)
    library.store.add_url("http://example.invalid/c.pkg")

    library.store.clear()

    assert library.store.urls() == []
    assert library.store.override(target["id"]) == {}


def test_item_id_is_case_insensitive_on_path():
    assert _item_id("C:/Games/A.pkg") == _item_id("c:/games/a.pkg")
