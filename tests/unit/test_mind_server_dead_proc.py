"""Tests for dead-subprocess handling in mind_server.send_message.

When the harness subprocess has exited, the message endpoint must NOT
return an opaque 500. It returns 404 with the exit code and captured
stderr tail so the NS gateway's respawn-on-404 path recreates the
session, and it removes the dead session from the registry.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _mock_mind_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MIND_ID", "ada")
    monkeypatch.setenv("MIND_NAME", "ada")
    # Module reload re-runs _setup_config_dir(); point it somewhere writable.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))


@pytest.fixture()
def client(_mock_mind_env):
    """TestClient for mind_server with CLI-mode impl (no send attribute)."""
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import importlib
            import mind_server
            importlib.reload(mind_server)
            # Force CLI path: impl must not have a `send` attribute
            mind_server.impl = MagicMock(spec=[])
            yield TestClient(mind_server.app, raise_server_exceptions=False), mind_server


def _dead_proc(returncode: int = -15):
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345
    proc.stdin = MagicMock()
    return proc


class TestDeadProcResponse:
    def test_dead_proc_returns_404_with_exit_code(self, client):
        test_client, mind_server = client
        mind_server._sessions["sess-dead"] = {
            "proc": _dead_proc(returncode=1),
            "model": "opus",
            "in_flight": False,
            "cancel_event": None,
            "stderr_tail": ["boom: API exploded", "second line"],
        }

        resp = test_client.post(
            "/sessions/sess-dead/message", json={"content": "hello"}
        )

        assert resp.status_code == 404
        err = resp.json()["error"]
        assert "returncode=1" in err
        assert "boom: API exploded" in err

    def test_dead_proc_removes_session(self, client):
        test_client, mind_server = client
        mind_server._sessions["sess-dead2"] = {
            "proc": _dead_proc(),
            "model": "opus",
            "in_flight": False,
            "cancel_event": None,
        }

        resp = test_client.post(
            "/sessions/sess-dead2/message", json={"content": "hello"}
        )

        assert resp.status_code == 404
        assert "sess-dead2" not in mind_server._sessions

    def test_dead_proc_without_stderr_tail_reports_placeholder(self, client):
        test_client, mind_server = client
        mind_server._sessions["sess-dead3"] = {
            "proc": _dead_proc(returncode=137),
            "model": "opus",
            "in_flight": False,
            "cancel_event": None,
        }

        resp = test_client.post(
            "/sessions/sess-dead3/message", json={"content": "hello"}
        )

        assert resp.status_code == 404
        err = resp.json()["error"]
        assert "returncode=137" in err
        assert "<no stderr captured>" in err

    def test_live_proc_still_streams(self, client):
        """Regression guard: a healthy session still gets a 200 SSE stream."""
        test_client, mind_server = client
        written = []
        stdin = MagicMock()
        stdin.write = lambda data: written.append(data)
        stdin.drain = AsyncMock()
        result_line = json.dumps({"type": "result", "session_id": "sid"}) + "\n"
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=[result_line.encode(), b""])
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = stdin
        proc.stdout = stdout
        mind_server._sessions["sess-live"] = {
            "proc": proc,
            "model": "opus",
            "in_flight": False,
            "cancel_event": None,
        }

        resp = test_client.post(
            "/sessions/sess-live/message", json={"content": "hello"}
        )

        assert resp.status_code == 200
        assert written
        mind_server._sessions.pop("sess-live", None)


class TestStderrDrain:
    def test_drain_captures_tail_and_exits_on_eof(self, client):
        _, mind_server = client
        proc = MagicMock()
        proc.pid = 999
        proc.returncode = 2
        proc.stderr = AsyncMock()
        proc.stderr.readline = AsyncMock(
            side_effect=[b"first error line\n", b"fatal: it died\n", b""]
        )
        session = {"proc": proc}

        asyncio.run(mind_server._drain_stderr(session, "sess-x"))

        assert list(session["stderr_tail"]) == [
            "first error line",
            "fatal: it died",
        ]

    def test_drain_noop_without_stderr(self, client):
        _, mind_server = client
        proc = MagicMock()
        proc.stderr = None
        session = {"proc": proc}

        asyncio.run(mind_server._drain_stderr(session, "sess-y"))

        assert "stderr_tail" not in session
