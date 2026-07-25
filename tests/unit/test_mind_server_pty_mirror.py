"""Tests for mirroring stream-json-surface (Telegram) turns into a live pty.

The browser terminal only shows what its own `claude` process drew. A turn delivered through the non-interactive stream-json path
(Telegram) never touches that process, so a browser tile watching the same
conversation would silently drift out of sync with what the user actually
said and heard. `_mirror_turn_to_pty` closes that gap by writing a rendered
copy of the turn to the session's attached socket, if one is open, wherever
assistant text is produced on that path: the live request reader, the idle
drain, and the pre-send unsolicited flush.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bots import proactive


@pytest.fixture()
def _mock_mind_env(monkeypatch):
    monkeypatch.setenv("MIND_ID", "ada")
    monkeypatch.setenv("MIND_NAME", "ada")


@pytest.fixture()
def mind_server_mod(_mock_mind_env):
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import importlib
            import mind_server
            importlib.reload(mind_server)
            mind_server.impl = MagicMock(spec=[])  # force CLI path
            yield mind_server
            mind_server._ptys.clear()


@pytest.fixture(autouse=True)
def _fresh_queue():
    proactive._reset()
    yield
    proactive._reset()


class _FakeHandle:
    """Stand-in for `_PtyHandle` — records what was sent without a real pty."""

    def __init__(self):
        self.fed: list[bytes] = []

    def push(self, data: bytes) -> None:
        self.fed.append(data)


# ---------------------------------------------------------------------------
# _mirror_turn_to_pty, direct
# ---------------------------------------------------------------------------

class TestMirrorTurnToPty:
    def test_no_session_id_is_a_noop(self, mind_server_mod):
        mind_server_mod._mirror_turn_to_pty(None, "hi", ["hello"])
        assert mind_server_mod._ptys == {}

    def test_no_assistant_text_is_a_noop(self, mind_server_mod):
        handle = _FakeHandle()
        mind_server_mod._ptys["sess-1"] = handle
        mind_server_mod._mirror_turn_to_pty("sess-1", "hi", [])
        assert handle.fed == []

    def test_no_pty_for_session_is_a_noop(self, mind_server_mod):
        # Nothing registered under "sess-1" — must not raise or fabricate one.
        mind_server_mod._mirror_turn_to_pty("sess-1", "hi", ["hello"])
        assert mind_server_mod._ptys == {}

    def test_feeds_both_sides_with_crlf_line_endings(self, mind_server_mod, monkeypatch):
        # Pin the label explicitly — config.py load_dotenv()s the install's
        # .env at import, so the ambient OWNER_NAME is whatever this host has.
        monkeypatch.setenv("OWNER_NAME", "testowner")
        handle = _FakeHandle()
        mind_server_mod._ptys["sess-1"] = handle

        mind_server_mod._mirror_turn_to_pty("sess-1", "what's the weather", ["sunny and 72"])

        assert len(handle.fed) == 1
        rendered = handle.fed[0].decode()
        assert "\r\n" in rendered  # terminal output needs CRLF, not bare LF
        assert "what's the weather" in rendered
        assert "sunny and 72" in rendered
        assert "testowner" in rendered  # OWNER_NAME labels the user's side
        assert "ada" in rendered  # MIND_NAME label

    def test_no_user_text_still_feeds_assistant_side(self, mind_server_mod, monkeypatch):
        """The idle-drain and flush paths have no user turn — just a spontaneous reply."""
        monkeypatch.setenv("OWNER_NAME", "testowner")
        handle = _FakeHandle()
        mind_server_mod._ptys["sess-1"] = handle

        mind_server_mod._mirror_turn_to_pty("sess-1", None, ["background result"])

        rendered = handle.fed[0].decode()
        assert "background result" in rendered
        assert "testowner:" not in rendered

    def test_multiple_text_blocks_joined(self, mind_server_mod):
        handle = _FakeHandle()
        mind_server_mod._ptys["sess-1"] = handle

        mind_server_mod._mirror_turn_to_pty("sess-1", None, ["first", "second"])

        rendered = handle.fed[0].decode()
        assert "first" in rendered
        assert "second" in rendered


# ---------------------------------------------------------------------------
# _idle_drain wiring
# ---------------------------------------------------------------------------

def _assistant_text_event(text: str) -> bytes:
    return (json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }) + "\n").encode()


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self._idle = asyncio.Event()

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await self._idle.wait()
        return b""


async def _drain_briefly(mind_server, session, ticks=0.4):
    task = asyncio.create_task(mind_server._idle_drain(session))
    await asyncio.sleep(ticks)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_idle_drain_mirrors_to_an_open_pty(mind_server_mod):
    handle = _FakeHandle()
    mind_server_mod._ptys["sess-1"] = handle
    proc = MagicMock()
    proc.stdout = _FakeStdout([_assistant_text_event("proactive update")])
    proc.returncode = None
    session = {
        "proc": proc,
        "session_id": "sess-1",
        "chat_id": 777,
        "in_flight": False,
        "stdout_lock": asyncio.Lock(),
        "cancel_event": asyncio.Event(),
    }

    await _drain_briefly(mind_server_mod, session)

    assert any(b"proactive update" in b for b in handle.fed)


async def test_idle_drain_mirrors_even_without_telegram_chat_id(mind_server_mod):
    """A terminal-only session (non-numeric client_ref) still gets mirrored."""
    handle = _FakeHandle()
    mind_server_mod._ptys["sess-1"] = handle
    proc = MagicMock()
    proc.stdout = _FakeStdout([_assistant_text_event("still shown")])
    proc.returncode = None
    session = {
        "proc": proc,
        "session_id": "sess-1",
        "chat_id": "terminal-9f2c1e40-0000-4000-8000-000000000000",
        "in_flight": False,
        "stdout_lock": asyncio.Lock(),
        "cancel_event": asyncio.Event(),
    }

    await _drain_briefly(mind_server_mod, session)

    assert any(b"still shown" in b for b in handle.fed)
    assert proactive._queue.empty()  # no numeric chat_id, so no Telegram delivery


async def test_idle_drain_without_a_pty_does_not_raise(mind_server_mod):
    proc = MagicMock()
    proc.stdout = _FakeStdout([_assistant_text_event("no terminal open")])
    proc.returncode = None
    session = {
        "proc": proc,
        "session_id": "sess-no-pty",
        "chat_id": 1,
        "in_flight": False,
        "stdout_lock": asyncio.Lock(),
        "cancel_event": asyncio.Event(),
    }

    await _drain_briefly(mind_server_mod, session)  # must not raise


# ---------------------------------------------------------------------------
# _flush_unsolicited wiring
# ---------------------------------------------------------------------------

def _result_event() -> bytes:
    return (json.dumps({"type": "result", "session_id": "abc"}) + "\n").encode()


async def test_flush_unsolicited_mirrors_stale_turn_to_pty(mind_server_mod):
    handle = _FakeHandle()
    mind_server_mod._ptys["sess-1"] = handle
    proc = MagicMock()
    proc.stdout = _FakeStdout([
        _assistant_text_event("background agent finished"),
        _result_event(),
    ])
    proc.returncode = None
    session = {
        "proc": proc,
        "session_id": "sess-1",
        "chat_id": 555,
        "in_flight": False,
        "stdout_lock": asyncio.Lock(),
        "cancel_event": asyncio.Event(),
    }

    await mind_server_mod._flush_unsolicited(session, proc, "sess-1")

    assert any(b"background agent finished" in b for b in handle.fed)


# ---------------------------------------------------------------------------
# End-to-end through /sessions/{id}/message
# ---------------------------------------------------------------------------

class _RealisticStdout:
    """Serves queued lines only after the first call — never immediately.

    `_flush_unsolicited` reads with a 0.2s timeout *before* the request is
    written to stdin. Against a real pipe that read finds nothing buffered
    and times out. A plain `AsyncMock(side_effect=[...])` resolves instantly
    regardless of ordering, so it would let the flush swallow the very lines
    meant to be this turn's response. Delaying the first call reproduces the
    real timing instead.
    """

    def __init__(self, lines):
        self._lines = list(lines)
        self._calls = 0

    async def readline(self):
        self._calls += 1
        if self._calls == 1:
            await asyncio.sleep(0.3)
        return self._lines.pop(0) if self._lines else b""


def _make_proc(written: list, assistant_text: str):
    mock_stdin = MagicMock()
    mock_stdin.write = lambda data: written.append(data)
    mock_stdin.drain = AsyncMock()

    lines = [
        _assistant_text_event(assistant_text),
        (json.dumps({"type": "result", "session_id": "test-sid"}) + "\n").encode(),
    ]
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stdin = mock_stdin
    mock_proc.stdout = _RealisticStdout(lines)
    return mock_proc


class TestSendMessageMirrorsToPty:
    def test_live_turn_is_mirrored_to_an_open_terminal(self, mind_server_mod):
        client = TestClient(mind_server_mod.app, raise_server_exceptions=False)
        handle = _FakeHandle()
        mind_server_mod._ptys["sess-msg"] = handle
        written: list = []
        mind_server_mod._sessions["sess-msg"] = {
            "proc": _make_proc(written, "the reply text"),
            "session_id": "sess-msg",
            "model": "sonnet",
            "in_flight": False,
            "cancel_event": None,
            "stdout_lock": asyncio.Lock(),
        }

        resp = client.post("/sessions/sess-msg/message", json={"content": "the question"})

        assert resp.status_code == 200
        rendered = b"".join(handle.fed).decode()
        assert "the question" in rendered
        assert "the reply text" in rendered

        mind_server_mod._sessions.pop("sess-msg", None)

    def test_no_open_terminal_is_unaffected(self, mind_server_mod):
        """The common case — no browser tile open — must not raise or change behavior."""
        client = TestClient(mind_server_mod.app, raise_server_exceptions=False)
        written: list = []
        mind_server_mod._sessions["sess-no-term"] = {
            "proc": _make_proc(written, "reply"),
            "session_id": "sess-no-term",
            "model": "sonnet",
            "in_flight": False,
            "cancel_event": None,
            "stdout_lock": asyncio.Lock(),
        }

        resp = client.post("/sessions/sess-no-term/message", json={"content": "hi"})

        assert resp.status_code == 200
        assert mind_server_mod._ptys == {}

        mind_server_mod._sessions.pop("sess-no-term", None)
