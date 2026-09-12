"""`GET /symbols` — a named function, resolved off this mind's own disk.

The resolution itself is covered in `test_code_symbols.py`. What this pins
is the route: that it is guarded like every other configuration route, that
it refuses rather than opening when no credential is configured, and — the
one that matters to the console — that a function which is simply absent
comes back as an unresolved answer with a 200 rather than an error.

The console reads a 404 on a route as "this mind predates the API" and
sends the operator off to redeploy. A typo in a function name must not
produce that sentence.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MIND_ID", "ada")
os.environ.setdefault("MIND_NAME", "example")
os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="symbols-route-test-"))

ADMIN_TOKEN = "not-a-real-token-symbols-route-test"  # secret-guard: allow

FIXTURE = "def helper(value):\n    return value + 1\n"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setenv("MIND_ID", "ada")
    monkeypatch.setenv("MIND_NAME", "ada")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("MIND_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(repo))
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import importlib

            import mind_server

            importlib.reload(mind_server)
            yield TestClient(mind_server.app, raise_server_exceptions=False), mind_server, repo


def _authed(http, **params):
    return http.get(
        "/symbols", params=params, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    )


def test_a_caller_with_no_credential_is_refused(client):
    http, _, repo = client

    response = http.get("/symbols", params={"path": "module.py", "name": "helper"})

    assert response.status_code == 401


def test_a_caller_with_the_wrong_credential_is_refused(client):
    http, _, repo = client

    response = http.get(
        "/symbols",
        params={"path": "module.py", "name": "helper"},
        headers={"Authorization": "Bearer not-the-token"},
    )

    assert response.status_code == 401


def test_a_mind_with_no_admin_credential_refuses_rather_than_opening(
    client, monkeypatch
):
    http, mind_server, _ = client
    monkeypatch.delenv("MIND_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(mind_server.runtime_config, "admin_token", lambda: "")

    response = http.get("/symbols", params={"path": "module.py", "name": "helper"})

    assert response.status_code == 503


def test_an_authorised_caller_gets_the_real_source(client):
    http, _, repo = client

    body = _authed(http, repo=str(repo), path="module.py", name="helper").json()

    assert body["resolved"] is True
    assert body["first_line"] == 1
    assert body["last_line"] == 2
    assert body["source"] == "def helper(value):\n    return value + 1"


def test_a_name_that_is_absent_is_an_answer_not_an_error(client):
    http, _, repo = client

    response = _authed(http, repo=str(repo), path="module.py", name="helpr")

    assert response.status_code == 200
    assert response.json()["resolved"] is False


def test_a_repository_this_mind_was_not_given_is_a_404(client, tmp_path):
    http, _, _ = client
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "module.py").write_text(FIXTURE, encoding="utf-8")

    response = _authed(http, repo=str(elsewhere), path="module.py", name="helper")

    assert response.status_code == 404


def test_the_roots_route_is_guarded_like_every_other_configuration_route(client):
    """A roots listing names every checkout path on the host, on a port that
    answers across the LAN."""
    http, mind_server, _ = client

    assert http.get("/symbols/roots").status_code == 401
    assert http.get(
        "/symbols/roots", headers={"Authorization": "Bearer not-the-token"}
    ).status_code == 401


def test_the_roots_route_reports_only_the_checkouts_that_are_actually_there(
    client, monkeypatch, tmp_path
):
    """A declared root that does not resolve is dropped rather than offered:
    an unmounted volume must not be reported as a repository this mind reads.
    Comparing the answer to the declared value alone would prove only that
    the route echoes its own environment back."""
    http, _, repo = client
    missing = tmp_path / "never-mounted"
    monkeypatch.setenv(
        "DESIGN_REPO_ROOTS", os.pathsep.join([str(repo), str(missing)])
    )

    response = http.get(
        "/symbols/roots", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    )

    assert response.status_code == 200
    assert response.json()["roots"] == [str(repo)]


def test_a_request_with_no_name_is_refused_as_a_bad_request(client):
    http, _, repo = client

    response = _authed(http, repo=str(repo), path="module.py")

    assert response.status_code == 400
