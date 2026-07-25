"""Tests for WS /sessions/{session_id}/attach-pty on mind_server.py.

Covers: bidirectional byte pumping between the browser WS and the pty
master fd, cleanup (proc terminated, fds closed) on disconnect, and the
graceful-refusal path when a mind's implementation has no spawn_pty.
"""
import os
import pty
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _mock_mind_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MIND_ID", "ada")
    monkeypatch.setenv("MIND_NAME", "ada")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))


@pytest.fixture()
def client(_mock_mind_env):
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import importlib
            import mind_server
            importlib.reload(mind_server)
            # The one piece of the mind's tmux plumbing that has to be real:
            # a name per session, or every handle claims the same terminal.
            mind_server.impl.tmux_session_name = lambda session_id: f"example-{session_id}"
            yield TestClient(mind_server.app, raise_server_exceptions=False), mind_server


def _fake_proc(pid: int = 4242):
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None  # still running until terminate() is observed
    return proc


class TestAttachPty:
    def test_refuses_when_impl_lacks_spawn_pty(self, client):
        test_client, mind_server = client
        mind_server.impl = MagicMock(spec=[])  # no spawn_pty attribute

        with test_client.websocket_connect("/sessions/sess-1/attach-pty?resume_sid=abc") as ws:
            with pytest.raises(Exception):
                ws.receive_bytes()

    def test_pumps_process_output_to_browser(self, client):
        test_client, mind_server = client
        master_fd, slave_fd = pty.openpty()
        proc = _fake_proc()
        mind_server.impl.spawn_pty = MagicMock(return_value=(proc, master_fd))

        with test_client.websocket_connect(
            "/sessions/sess-1/attach-pty?resume_sid=abc&model=sonnet"
        ) as ws:
            # The pty line discipline runs even with no child attached to the
            # slave side, so a bare \n written here comes out \r\n on master.
            os.write(slave_fd, b"hello from the tui\n")
            received = ws.receive_bytes()
            assert received == b"hello from the tui\r\n"

        mind_server.impl.spawn_pty.assert_called_once()
        kwargs = mind_server.impl.spawn_pty.call_args.kwargs
        assert kwargs["resume_sid"] == "abc"
        assert kwargs["model"] == "sonnet"
        os.close(slave_fd)

    def test_pumps_browser_input_to_process(self, client):
        test_client, mind_server = client
        master_fd, slave_fd = pty.openpty()
        proc = _fake_proc()
        mind_server.impl.spawn_pty = MagicMock(return_value=(proc, master_fd))

        with test_client.websocket_connect("/sessions/sess-2/attach-pty?resume_sid=abc") as ws:
            ws.send_bytes(b"/help\n")
            time.sleep(0.1)
            assert os.read(slave_fd, 4096) == b"/help\n"

        os.close(slave_fd)

    def test_disconnect_leaves_the_conversation_running(self, client):
        """Closing a browser tile ends the view, not the conversation.

        The socket is an observer: its client dies, `claude` keeps running
        inside tmux. When the socket owned the process, switching tiles
        mid-turn threw the turn away and reattaching showed the question
        with no answer under it.
        """
        test_client, mind_server = client
        master_fd, slave_fd = pty.openpty()
        proc = _fake_proc()
        mind_server.impl.spawn_pty = MagicMock(return_value=(proc, master_fd))

        with test_client.websocket_connect("/sessions/sess-3/attach-pty?resume_sid=abc"):
            pass

        mind_server.impl.kill_pty_session.assert_not_called()
        assert "sess-3" in mind_server._ptys
        os.close(slave_fd)

    def test_reattach_returns_to_the_same_conversation(self, client):
        """A second attach starts a second *client* on the terminal that is
        already running — never a second `claude` on the same transcript,
        which is how two tiles used to cross each other's threads."""
        test_client, mind_server = client
        fds = [pty.openpty(), pty.openpty()]
        procs = [_fake_proc(1), _fake_proc(2)]
        spawn = MagicMock(side_effect=[(procs[0], fds[0][0]), (procs[1], fds[1][0])])
        mind_server.impl.spawn_pty = spawn

        with test_client.websocket_connect("/sessions/sess-6/attach-pty?resume_sid=abc"):
            pass
        with test_client.websocket_connect("/sessions/sess-6/attach-pty?resume_sid=abc"):
            pass

        assert len(mind_server._ptys) == 1
        assert mind_server._ptys["sess-6"].claude_sid == "abc"
        mind_server.impl.kill_pty_session.assert_not_called()
        for _, slave_fd in fds:
            os.close(slave_fd)

    def test_keepalive_heartbeat_reaches_the_browser(self, client):
        """A NUL beats down the socket so the client can spot a half-open
        (zombie) connection instead of sitting black for the browser's
        whole ~30s dead-connection timeout."""
        test_client, mind_server = client
        master_fd, slave_fd = pty.openpty()
        proc = _fake_proc()
        mind_server.impl.spawn_pty = MagicMock(return_value=(proc, master_fd))
        mind_server._PTY_KEEPALIVE_S = 0.05  # beat fast so the test is quick

        with test_client.websocket_connect("/sessions/sess-ka/attach-pty?resume_sid=abc") as ws:
            saw_beat = False
            for _ in range(30):
                if ws.receive_bytes() == mind_server._PTY_KEEPALIVE_BYTE:
                    saw_beat = True
                    break
            assert saw_beat

        os.close(slave_fd)

    def test_two_sessions_never_share_a_terminal(self, client):
        """The thread-crossing bug: distinct sessions must get distinct ptys."""
        test_client, mind_server = client
        fds = [pty.openpty(), pty.openpty()]
        procs = [_fake_proc(1), _fake_proc(2)]
        mind_server.impl.spawn_pty = MagicMock(side_effect=[
            (procs[0], fds[0][0]),
            (procs[1], fds[1][0]),
        ])

        with test_client.websocket_connect("/sessions/sess-a/attach-pty?resume_sid=sid-a"):
            pass
        with test_client.websocket_connect("/sessions/sess-b/attach-pty?resume_sid=sid-b"):
            pass

        assert mind_server._ptys["sess-a"].tmux_name != mind_server._ptys["sess-b"].tmux_name
        assert mind_server._ptys["sess-a"].claude_sid == "sid-a"
        assert mind_server._ptys["sess-b"].claude_sid == "sid-b"
        for _, slave_fd in fds:
            os.close(slave_fd)

    def test_killing_the_session_terminates_the_terminal(self, client):
        test_client, mind_server = client
        master_fd, slave_fd = pty.openpty()
        proc = _fake_proc()
        mind_server.impl.spawn_pty = MagicMock(return_value=(proc, master_fd))

        with test_client.websocket_connect("/sessions/sess-7/attach-pty?resume_sid=abc"):
            pass
        resp = test_client.delete("/sessions/sess-7")

        assert resp.status_code == 200
        proc.terminate.assert_called_once()
        assert "sess-7" not in mind_server._ptys
        os.close(slave_fd)

    def test_attach_without_a_conversation_id_is_refused(self, client):
        """A session that appears attachable but hands over no conversation
        must not open as a blank terminal — that is the bug, not the fix.
        The mind refuses; nothing is spawned."""
        test_client, mind_server = client
        spawn = MagicMock()
        mind_server.impl.spawn_pty = spawn

        with test_client.websocket_connect("/sessions/sess-8/attach-pty") as ws:
            with pytest.raises(Exception):
                ws.receive_bytes()

        spawn.assert_not_called()
        assert "sess-8" not in mind_server._ptys
