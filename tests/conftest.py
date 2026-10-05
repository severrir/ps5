"""Shared fixtures.

Everything that touches disk is redirected into a per-test temporary directory by
rebinding the module-level paths in :mod:`server.config`. That keeps the suite from
reading or writing the real ``configs.json``, ``data/`` or ``logs/`` -- which matters
more than usual here, because one of the modules under test deletes files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import config as config_module  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every server path at a throwaway tree and return it."""
    app_root = tmp_path / "hub"
    for name in ("server", "static", "templates", "tests", "pkg_wrapper"):
        (app_root / name).mkdir(parents=True, exist_ok=True)
    (app_root / "run.py").write_text("# sentinel\n", encoding="utf-8")
    (app_root / "requirements.txt").write_text("flask\n", encoding="utf-8")

    monkeypatch.setattr(config_module, "APP_ROOT", app_root)
    monkeypatch.setattr(config_module, "CONFIG_PATH", app_root / "configs.json")
    monkeypatch.setattr(config_module, "EXAMPLE_PATH", app_root / "configs.example.json")
    monkeypatch.setattr(config_module, "DATA_DIR", app_root / "data")
    monkeypatch.setattr(config_module, "LOG_DIR", app_root / "logs")
    monkeypatch.setattr(config_module, "COVERS_DIR", app_root / "covers")
    monkeypatch.setattr(config_module, "BGM_DIR", app_root / "bgm")
    monkeypatch.setattr(
        config_module,
        "PROTECTED_PATHS",
        (
            app_root / "server",
            app_root / "static",
            app_root / "templates",
            app_root / "tests",
            app_root / "pkg_wrapper",
            app_root / "run.py",
            app_root / "requirements.txt",
            app_root / "configs.example.json",
            app_root / "configs.json",
        ),
    )

    config_module.reset_cache()
    config_module.load(force_reload=True)
    yield app_root
    config_module.reset_cache()


@pytest.fixture
def packages_dir(sandbox):
    """A package directory wired into the sandboxed config."""
    directory = sandbox / "packages"
    directory.mkdir(parents=True, exist_ok=True)
    config_module.save({"library": {"pkg_directories": [str(directory)]}})
    return directory


@pytest.fixture
def config():
    return config_module
