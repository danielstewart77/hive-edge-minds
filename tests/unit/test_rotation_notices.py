"""A rotated terminal tells the user what happened to it.

`respawn-pane -k` takes the pane's history with it, so before this the tile
went blank mid-conversation with nothing to say why — and neither harness can
be told to print the recap itself: `claude` runs on the alternate screen and
`codex` paints over anything written ahead of its exec. A tmux popup is drawn
by the client, over whatever the pane's app is doing, which is why the
notices go through one.

Six behaviours, one per requirement:

1. a rotation that has begun tells an attached terminal it is rotating
2. the terminal that comes back leads with "this session has been rotated"
3. the last exchange before the rotation is replayed under that line
4. a very long reply is trimmed to its last 50 lines
5. with no prior exchange, the rotation line stands alone
6. codex draws the same notices as claude

The recap is composed in ``mind_server`` and handed to whichever template is
loaded, so 2-5 exercise the route and 6 pins the codex template to the same
popup the claude one gets.
"""

import importlib
import pty
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import pty_notice

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MIND_ID", "skippy")
    monkeypatch.setenv("MIND_NAME", "skippy")
    monkeypatch.setenv("OWNER_NAME", "daniel")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    with patch.dict("sys.modules", {"minds.skippy.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import mind_server
            importlib.reload(mind_server)
            mind_server.impl.tmux_session_name = lambda sid: f"skippy-{sid}"
            yield TestClient(mind_server.app, raise_server_exceptions=False), mind_server


def _attached(mind_server, test_client, session_id: str):
    """Give a session a live terminal, the way an open tile would."""
    master_fd, slave_fd = pty.openpty()
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    mind_server.impl.spawn_pty = MagicMock(return_value=(proc, master_fd))
    with test_client.websocket_connect(
        f"/sessions/{session_id}/attach-pty?model=sonnet&resume_sid=conv-1"
    ):
        pass
    return slave_fd


def _notice(impl, session_id: str, *, hold: bool) -> str:
    """The text show_pty_notice was asked to draw, for this session.

    Asserts the target and the dismissal too: a notice drawn on the wrong
    session, or one that closes itself while the user is still reading it,
    is a notice they never see.
    """
    assert impl.show_pty_notice.call_args, "no notice was drawn"
    args, kwargs = impl.show_pty_notice.call_args
    assert args[0] == session_id, f"notice drawn on {args[0]}, not {session_id}"
    assert kwargs.get("hold") is hold
    return args[1]


# --- template-level plumbing ----------------------------------------------

def _load_template(name: str):
    """Import a harness template the way `setup.sh` instantiates it."""
    sys.path.insert(0, str(REPO_ROOT))
    return importlib.reload(importlib.import_module(f"mind_templates.{name}"))


def _popen_recorder(monkeypatch, module):
    """Record the tmux argv a template launches, without running tmux.

    Popen rather than the template's ``_tmux`` helper: a popup must never be
    waited on, so it deliberately does not go through the helper that does.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module.subprocess, "Popen",
        lambda argv, **kw: calls.append(list(argv)),
    )
    return calls


def _popup_body(calls: list[list[str]]) -> str:
    """The text a recorded display-popup call would have shown."""
    for argv in calls:
        if "display-popup" not in argv:
            continue
        for token in argv[-1].split():
            if token.strip("';").endswith(".popup"):
                return Path(token.strip("';")).read_text()
    raise AssertionError(f"no display-popup body among {calls}")


# 1 ------------------------------------------------------------------------
def test_a_rotation_that_has_begun_tells_the_attached_terminal(client):
    test_client, mind_server = client
    slave_fd = _attached(mind_server, test_client, "sess-1")
    mind_server.impl.show_pty_notice = MagicMock(return_value=True)

    resp = test_client.post(
        "/sessions/sess-1/pty-notice", json={"text": pty_notice.ROTATING_TEXT},
    )

    assert resp.json() == {"session_id": "sess-1", "shown": True}
    assert _notice(mind_server.impl, "sess-1", hold=False) == pty_notice.ROTATING_TEXT
    # Nobody watching is the common case: the mind says so and the rotation
    # about to follow is not affected.
    mind_server.impl.show_pty_notice = MagicMock(return_value=False)
    resp = test_client.post(
        "/sessions/ghost/pty-notice", json={"text": pty_notice.ROTATING_TEXT},
    )
    assert resp.json() == {"session_id": "ghost", "shown": False}
    __import__("os").close(slave_fd)


# 2 ------------------------------------------------------------------------
def test_the_rotated_terminal_leads_with_the_rotation_line(client):
    test_client, mind_server = client
    slave_fd = _attached(mind_server, test_client, "sess-2")
    mind_server.impl.rotate_pty_session = MagicMock(return_value=True)
    mind_server.impl.show_pty_notice = MagicMock(return_value=True)

    test_client.post(
        "/sessions/sess-2/rotate-pty",
        json={"new_claude_sid": "conv-2", "model": "sonnet",
              "last_exchange": {"user": "what broke?", "assistant": "the pane did"}},
    )

    body = _notice(mind_server.impl, "sess-2", hold=True)
    assert pty_notice.ROTATED_TEXT in body
    assert body.index(pty_notice.ROTATED_TEXT) < body.index("what broke?")

    # A rotation that did not happen has nothing to announce: saying it did
    # would tell the user their context reset when it did not.
    mind_server.impl.rotate_pty_session = MagicMock(return_value=False)
    mind_server.impl.show_pty_notice = MagicMock(return_value=True)
    test_client.post(
        "/sessions/sess-2/rotate-pty",
        json={"new_claude_sid": "conv-3", "model": "sonnet"},
    )
    mind_server.impl.show_pty_notice.assert_not_called()
    __import__("os").close(slave_fd)


# 3 ------------------------------------------------------------------------
def test_the_last_exchange_is_replayed_under_that_line(client):
    test_client, mind_server = client
    slave_fd = _attached(mind_server, test_client, "sess-3")
    mind_server.impl.rotate_pty_session = MagicMock(return_value=True)
    mind_server.impl.show_pty_notice = MagicMock(return_value=True)

    test_client.post(
        "/sessions/sess-3/rotate-pty",
        json={"new_claude_sid": "conv-3", "model": "sonnet",
              "last_exchange": {
                  "user": "why is the terminal blank",
                  "assistant": "because respawn-pane drops the history"}},
    )

    body = _notice(mind_server.impl, "sess-3", hold=True)
    assert "why is the terminal blank" in body
    assert "because respawn-pane drops the history" in body
    __import__("os").close(slave_fd)


# 4 ------------------------------------------------------------------------
def test_a_very_long_reply_is_trimmed_to_its_last_fifty_lines(client):
    test_client, mind_server = client
    slave_fd = _attached(mind_server, test_client, "sess-4")
    mind_server.impl.rotate_pty_session = MagicMock(return_value=True)
    mind_server.impl.show_pty_notice = MagicMock(return_value=True)
    reply = "\n".join(f"line-{n}" for n in range(200))

    test_client.post(
        "/sessions/sess-4/rotate-pty",
        json={"new_claude_sid": "conv-4", "model": "sonnet",
              "last_exchange": {"user": "dump it", "assistant": reply}},
    )

    body = _notice(mind_server.impl, "sess-4", hold=True)
    assert "line-199" in body and "line-150" in body
    assert "line-149" not in body and "line-1\n" not in body
    __import__("os").close(slave_fd)


# 5 ------------------------------------------------------------------------
def test_with_no_prior_exchange_the_rotation_line_stands_alone(client):
    test_client, mind_server = client
    slave_fd = _attached(mind_server, test_client, "sess-5")
    mind_server.impl.rotate_pty_session = MagicMock(return_value=True)
    mind_server.impl.show_pty_notice = MagicMock(return_value=True)

    test_client.post(
        "/sessions/sess-5/rotate-pty",
        json={"new_claude_sid": "conv-5", "model": "sonnet"},
    )

    body = _notice(mind_server.impl, "sess-5", hold=True)
    assert pty_notice.ROTATED_TEXT in body
    assert body.strip().endswith(f"── {pty_notice.ROTATED_TEXT} ──\x1b[0m")
    __import__("os").close(slave_fd)


# 6 ------------------------------------------------------------------------
@pytest.mark.parametrize("template", ["claude_cli", "codex_cli"])
def test_codex_draws_the_same_notices_as_claude(template, monkeypatch, tmp_path):
    # Same call, same popup, whichever harness owns the pane — the templates
    # only supply the tmux socket, never the wording.
    module = _load_template(template)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(module, "CODEX_HOME", tmp_path, raising=False)
    monkeypatch.setattr(module, "pty_session_alive", lambda sid: True)
    calls = _popen_recorder(monkeypatch, module)

    recap = pty_notice.render_recap(
        {"user": "same for codex?", "assistant": "same for codex"},
        user_label="daniel", assistant_label="skippy",
    )
    assert module.show_pty_notice("sess-6", recap, hold=True) is True

    body = _popup_body(calls)
    assert pty_notice.ROTATED_TEXT in body
    assert "same for codex?" in body and "same for codex" in body
