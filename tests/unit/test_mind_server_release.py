"""One conversation, one live process — `POST /sessions/{id}/release`.

Two harness processes on one conversation both hold it in memory, neither
sees the other's turns, and each appends turns the other doesn't know
happened. So moving a session between the browser terminal and Telegram
means ending the outgoing process first. Release ends one surface and
leaves the conversation itself alone: the transcript is the handover
medium, and the incoming surface resumes from it.
"""
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MIND_ID", "test-mind-id")
os.environ.setdefault("MIND_NAME", "example")
os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="release-test-"))


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MIND_ID", "ada")
    monkeypatch.setenv("MIND_NAME", "ada")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import importlib
            import mind_server
            importlib.reload(mind_server)
            mind_server.impl.tmux_session_name = lambda session_id: f"example-{session_id}"
            mind_server.impl.kill_pty_session = MagicMock(return_value=True)
            yield TestClient(mind_server.app, raise_server_exceptions=False), mind_server
            mind_server._ptys.clear()
            mind_server._sessions.clear()


def _register_terminal(mind_server, session_id: str):
    handle = mind_server._PtyHandle(session_id, f"example-{session_id}", "conv-1", 80, 24)
    mind_server._ptys[session_id] = handle
    return handle


def _register_stream(mind_server, session_id: str):
    proc = MagicMock()
    proc.returncode = None
    proc.terminate = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    mind_server._sessions[session_id] = {"proc": proc, "model": "sonnet"}
    return proc


def test_releasing_the_terminal_leaves_the_stream_process_alone(client):
    test_client, mind_server = client
    _register_terminal(mind_server, "sess-1")
    proc = _register_stream(mind_server, "sess-1")

    resp = test_client.post("/sessions/sess-1/release?surface=terminal")

    assert resp.status_code == 200
    assert resp.json()["released"] is True
    mind_server.impl.kill_pty_session.assert_called_once_with("sess-1")
    assert "sess-1" not in mind_server._ptys
    assert "sess-1" in mind_server._sessions  # the other surface keeps running
    proc.terminate.assert_not_called()


def test_releasing_the_stream_leaves_the_terminal_alone(client):
    test_client, mind_server = client
    _register_terminal(mind_server, "sess-2")
    _register_stream(mind_server, "sess-2")

    resp = test_client.post("/sessions/sess-2/release?surface=stream")

    assert resp.status_code == 200
    assert resp.json()["released"] is True
    assert "sess-2" not in mind_server._sessions
    assert "sess-2" in mind_server._ptys
    mind_server.impl.kill_pty_session.assert_not_called()


def test_releasing_a_surface_that_is_not_running_is_not_an_error(client):
    # Handover shouldn't have to know which surface was live — the answer
    # is the response, not an exception.
    test_client, mind_server = client

    resp = test_client.post("/sessions/never-existed/release?surface=stream")

    assert resp.status_code == 200
    assert resp.json()["released"] is False


def test_unknown_surface_is_refused(client):
    test_client, mind_server = client

    resp = test_client.post("/sessions/sess-3/release?surface=carrier-pigeon")

    assert resp.status_code == 400


def test_kill_still_ends_both_surfaces(client):
    # Release is for handover; DELETE is still the end of the conversation.
    test_client, mind_server = client
    mind_server.impl.kill = AsyncMock()
    _register_terminal(mind_server, "sess-4")
    proc = _register_stream(mind_server, "sess-4")

    resp = test_client.delete("/sessions/sess-4")

    assert resp.status_code == 200
    assert "sess-4" not in mind_server._ptys
    assert "sess-4" not in mind_server._sessions
    mind_server.impl.kill_pty_session.assert_called_once_with("sess-4")
    mind_server.impl.kill.assert_awaited_once_with(proc)


def test_kill_of_an_unknown_session_still_404s(client):
    test_client, mind_server = client

    resp = test_client.delete("/sessions/nothing-here")

    assert resp.status_code == 404
