"""configured_surfaces() reads the runtime.yaml surfaces list."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def launcher(monkeypatch):
    monkeypatch.setenv("MIND_ID", "test-mind-id")
    monkeypatch.setenv("MIND_NAME", "example")
    # mind_server import is heavy; the launcher only needs its app attr.
    monkeypatch.setitem(sys.modules, "mind_server", MagicMock(app=MagicMock()))
    sys.modules.pop("launch_mind_server_and_bots", None)
    sys.path.insert(0, str(REPO_ROOT))
    import launch_mind_server_and_bots as mod
    yield mod
    sys.modules.pop("launch_mind_server_and_bots", None)


def _write_runtime(tmp_path, monkeypatch, launcher, content):
    mind_dir = tmp_path / "minds" / "testmind"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text(content)
    monkeypatch.setenv("MIND_NAME", "testmind")
    monkeypatch.setattr(launcher, "PROJECT_DIR", tmp_path)


def test_reads_surfaces_from_runtime_yaml(launcher, tmp_path, monkeypatch):
    _write_runtime(tmp_path, monkeypatch, launcher,
                   "name: testmind\nsurfaces:\n  - telegram\n  - discord\n")
    assert launcher.configured_surfaces() == ["telegram", "discord"]


def test_empty_surfaces_means_no_bots(launcher, tmp_path, monkeypatch):
    _write_runtime(tmp_path, monkeypatch, launcher,
                   "name: testmind\nsurfaces: []\n")
    assert launcher.configured_surfaces() == []


def test_unknown_surfaces_are_dropped(launcher, tmp_path, monkeypatch):
    _write_runtime(tmp_path, monkeypatch, launcher,
                   "name: testmind\nsurfaces:\n  - telegram\n  - carrier-pigeon\n")
    assert launcher.configured_surfaces() == ["telegram"]


def test_missing_runtime_yaml_falls_back_to_telegram(launcher, monkeypatch):
    monkeypatch.setenv("MIND_NAME", "no-such-mind")
    assert launcher.configured_surfaces() == ["telegram"]


def test_runtime_without_surfaces_key_falls_back_to_telegram(launcher, tmp_path, monkeypatch):
    _write_runtime(tmp_path, monkeypatch, launcher, "name: testmind\n")
    assert launcher.configured_surfaces() == ["telegram"]
