"""The contract this mind's `/skills` API owes the console.

There are two independent `mind_server` implementations — this one for edge
minds and `hive_mind`'s for the container minds — and they must never share
code. So each one proves the same contract separately: the console asks one
set of questions and gets one shape of answer back, whichever it is talking
to.

Nothing here monkeypatches `repo_root`, `installed_root` or `_harness`.
Those three *are* the contract — which directory a harness reads is the
whole question — so the fixtures move `PROJECT_DIR` and the environment
instead, and let the real code compute the paths.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MIND_ID", "test-mind-id")
os.environ.setdefault("MIND_NAME", "example")
os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="skills-test-"))

import skills_sync  # noqa: E402

ADMIN_TOKEN = "test-admin-token"  # secret-guard: allow

# The loaded mind_server, captured by the fixture. Re-importing it in a
# test would build a second module with its own runtime_config, and a
# patch applied there would never reach the app the client is bound to.
SERVER = None


def _write_skill(root, name: str, body: str, **extra: str) -> None:
    """A skill is a directory; `extra` puts sibling files beside the markdown."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body)
    for filename, content in extra.items():
        (directory / filename).write_text(content)


def _build(monkeypatch, tmp_path, harness: str):
    """A mind of the given harness, with real path resolution."""
    project = tmp_path / "project"
    config = tmp_path / "config"
    (project / "specs" / "skills" / "claude").mkdir(parents=True)
    (project / "specs" / "skills" / "codex").mkdir(parents=True)
    (config / "skills").mkdir(parents=True)

    monkeypatch.setenv("MIND_ID", "example")
    monkeypatch.setenv("MIND_NAME", "example")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CODEX_HOME", str(config))
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(skills_sync, "PROJECT_DIR", project, raising=True)

    with patch.dict("sys.modules", {"minds.example.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import importlib

            import mind_server

            importlib.reload(mind_server)
            global SERVER
            SERVER = mind_server
            # runtime.yaml is what names the harness, so stub the file read
            # rather than the function that interprets it.
            monkeypatch.setattr(
                mind_server.runtime_config,
                "load_runtime",
                lambda name: {"name": name, "harness": harness},
                raising=True,
            )
            client = TestClient(mind_server.app, raise_server_exceptions=False)
            return client, skills_sync.repo_root(harness), config / "skills"


@pytest.fixture()
def mind(monkeypatch, tmp_path):
    yield _build(monkeypatch, tmp_path, "claude_cli")


@pytest.fixture()
def codex_mind(monkeypatch, tmp_path):
    yield _build(monkeypatch, tmp_path, "codex_cli")


def _auth() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def test_the_skills_route_reports_both_sides_and_every_state(mind):
    """All four states, computed by the real state machine."""
    client, repo, installed = mind
    _write_skill(repo, "in-sync", "shared\n")
    _write_skill(installed, "in-sync", "shared\n")
    _write_skill(repo, "edited", "repo body\n")
    _write_skill(installed, "edited", "mind body\n")
    _write_skill(repo, "absent-here", "repo only\n")
    _write_skill(installed, "mind-only", "mind only\n")

    body = client.get("/skills", headers=_auth()).json()

    assert body["harness"] == "claude"
    rows = {row["name"]: row for row in body["skills"]}
    assert rows["in-sync"]["state"] == skills_sync.STATE_SAME
    assert rows["edited"]["state"] == skills_sync.STATE_DIFFERS
    assert rows["absent-here"]["state"] == skills_sync.STATE_NOT_INSTALLED
    assert rows["mind-only"]["state"] == skills_sync.STATE_LOCAL_ONLY
    assert rows["edited"]["repo"] == "repo body\n"
    assert rows["edited"]["installed"] == "mind body\n"


def test_a_codex_mind_reads_the_codex_directories(codex_mind, tmp_path):
    """The harness picks both the source directory and the installed one."""
    client, repo, installed = codex_mind
    assert repo == tmp_path / "project" / "specs" / "skills" / "codex"
    _write_skill(repo, "codex-skill", "for codex\n")
    _write_skill(tmp_path / "project" / "specs" / "skills" / "claude", "claude-skill", "x\n")

    body = client.get("/skills", headers=_auth()).json()

    assert body["harness"] == "codex"
    assert [row["name"] for row in body["skills"]] == ["codex-skill"]


def test_a_sibling_file_drifting_is_not_reported_as_in_sync(mind):
    """State is the whole directory. A matching SKILL.md is not enough."""
    client, repo, installed = mind
    _write_skill(repo, "memory", "same markdown\n", **{"helper.py": "repo version\n"})
    _write_skill(installed, "memory", "same markdown\n", **{"helper.py": "mind version\n"})

    row = client.get("/skills", headers=_auth()).json()["skills"][0]

    assert row["state"] == skills_sync.STATE_DIFFERS


def test_installing_moves_the_whole_directory_not_just_the_markdown(mind):
    client, repo, installed = mind
    _write_skill(repo, "memory", "body\n", **{"helper.py": "repo version\n"})
    _write_skill(installed, "memory", "body\n", **{"helper.py": "mind version\n"})

    client.post("/skills/memory/install", headers=_auth())

    assert (installed / "memory" / "helper.py").read_text() == "repo version\n"


def test_the_diff_names_both_sides_and_shows_the_change(mind):
    client, repo, installed = mind
    _write_skill(repo, "memory", "line one\nrepo line\n")
    _write_skill(installed, "memory", "line one\nmind line\n")

    diff = client.get("/skills/memory/diff", headers=_auth()).json()["diff"]

    assert "--- repo/memory/SKILL.md" in diff
    assert "+++ mind/memory/SKILL.md" in diff
    assert "-repo line" in diff
    assert "+mind line" in diff


def test_write_back_and_remove_answer_the_shape_the_console_expects(mind):
    client, repo, installed = mind
    _write_skill(repo, "memory", "repo body\n")
    _write_skill(installed, "memory", "mind body\n")

    written = client.post("/skills/memory/write-back", headers=_auth())
    assert written.status_code == 200
    assert written.json()["skill"]["state"] == skills_sync.STATE_SAME
    assert (repo / "memory" / "SKILL.md").read_text() == "mind body\n"

    removed = client.delete("/skills/memory", headers=_auth())
    assert removed.status_code == 200
    assert not (installed / "memory").exists()
    assert (repo / "memory" / "SKILL.md").exists()


def test_an_unreadable_skill_is_not_reported_as_an_absent_one(mind):
    """The remedy offered for "absent" overwrites the directory."""
    client, repo, installed = mind
    _write_skill(repo, "memory", "repo body\n")
    _write_skill(installed, "memory", "mind body\n")
    (installed / "memory").chmod(0o000)
    try:
        row = client.get("/skills", headers=_auth()).json()["skills"][0]
    finally:
        (installed / "memory").chmod(0o755)

    assert row["state"] == skills_sync.STATE_UNREADABLE


def test_an_unreadable_skills_directory_is_not_reported_as_an_empty_one(mind):
    client, repo, installed = mind
    installed.chmod(0o000)
    try:
        response = client.get("/skills", headers=_auth())
    finally:
        installed.chmod(0o755)

    assert response.status_code == 503
    assert "cannot be listed" in response.json()["error"]


def test_a_symlinked_skill_can_be_removed_and_replaced(mind, tmp_path):
    """hive_mind's curator maintains plugin skills as symlinks."""
    client, repo, installed = mind
    real = tmp_path / "plugin" / "notify"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("from a plugin\n")
    (installed / "notify").symlink_to(real)
    _write_skill(repo, "notify", "from the repo\n")

    assert client.post("/skills/notify/install", headers=_auth()).status_code == 200
    assert not (installed / "notify").is_symlink()
    assert (installed / "notify" / "SKILL.md").read_text() == "from the repo\n"
    assert (real / "SKILL.md").read_text() == "from a plugin\n"

    assert client.delete("/skills/notify", headers=_auth()).status_code == 200


def test_a_skill_carrying_a_build_directory_is_refused_rather_than_copied(mind):
    """Following a venv into the repo is hundreds of megabytes git ignores."""
    client, repo, installed = mind
    _write_skill(installed, "heavy", "body\n")
    (installed / "heavy" / "venv").mkdir()
    (installed / "heavy" / "venv" / "blob").write_bytes(
        b"x" * (skills_sync.MAX_SKILL_BYTES + 1)
    )

    response = client.post("/skills/heavy/write-back", headers=_auth())

    assert response.status_code == 404
    assert "larger than a skill should be" in response.json()["error"]
    assert not (repo / "heavy").exists()


def test_a_malformed_runtime_file_is_reported_rather_than_crashing(mind, monkeypatch):
    client, _, _ = mind

    def _boom(name):
        raise ValueError("Invalid mind name")

    monkeypatch.setattr(SERVER.runtime_config, "load_runtime", _boom, raising=True)

    response = client.get("/skills", headers=_auth())

    assert response.status_code == 500
    assert response.json()["error"] == "Invalid mind name"


def test_every_route_requires_the_admin_bearer(mind):
    """Reads included — these return the full text of every skill."""
    client, repo, _ = mind
    _write_skill(repo, "memory", "repo body\n")

    for call in (
        lambda: client.get("/skills"),
        lambda: client.get("/skills/memory/diff"),
        lambda: client.post("/skills/memory/install"),
        lambda: client.post("/skills/memory/write-back"),
        lambda: client.delete("/skills/memory"),
    ):
        assert call().status_code == 401
