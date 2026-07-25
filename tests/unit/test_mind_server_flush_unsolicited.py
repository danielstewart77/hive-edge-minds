"""Unit tests for the pre-send flush of unsolicited harness output.

A completed background task notification is a full harness turn: assistant
text followed by its own ``result`` event. If that output is still sitting in
the pipe when the next user message is written, the request reader stops at
*that* ``result``, hands the notification back as the answer, and leaves the
real reply for whatever the user sends next — a permanent one-turn lag.

``_flush_unsolicited`` drains that stale output before the write, routing
assistant text to the proactive queue when the surface can deliver it.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

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
            yield mind_server


@pytest.fixture(autouse=True)
def _fresh_queue():
    proactive._reset()
    yield
    proactive._reset()


def _assistant_text_event(text: str) -> bytes:
    return (json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }) + "\n").encode()


def _result_event() -> bytes:
    return (json.dumps({"type": "result", "session_id": "abc"}) + "\n").encode()


class _FakeStdout:
    """Serves queued byte lines, then blocks — a live but silent subprocess."""

    def __init__(self, lines):
        self._lines = list(lines)
        self._idle = asyncio.Event()

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await self._idle.wait()
        return b""


def _make_session(lines, chat_id=555):
    proc = MagicMock()
    proc.stdout = _FakeStdout(lines)
    proc.returncode = None
    return {
        "proc": proc,
        "model": "sonnet",
        "chat_id": chat_id,
        "in_flight": False,
        "stdout_lock": asyncio.Lock(),
        "cancel_event": asyncio.Event(),
    }


async def test_stale_notification_turn_is_consumed_before_send(mind_server_mod):
    """The whole stale turn, result event included, is drained."""
    session = _make_session([
        _assistant_text_event("background agent finished"),
        _result_event(),
    ])
    consumed = await mind_server_mod._flush_unsolicited(
        session, session["proc"], "sess-1"
    )
    assert consumed == 2
    # Nothing left to satisfy the request that is about to be written.
    remaining = session["proc"].stdout._lines
    assert remaining == []


async def test_flushed_assistant_text_goes_to_proactive_queue(mind_server_mod):
    """Unsolicited text is delivered, not silently dropped, on a chat surface."""
    session = _make_session([_assistant_text_event("unprompted thought")], chat_id=777)
    await mind_server_mod._flush_unsolicited(session, session["proc"], "sess-1")
    assert not proactive._queue.empty()
    assert await proactive.get() == (777, "unprompted thought")


async def test_quiet_pipe_flushes_nothing(mind_server_mod):
    """The common case — no stale output — consumes nothing and returns fast."""
    session = _make_session([])
    consumed = await asyncio.wait_for(
        mind_server_mod._flush_unsolicited(session, session["proc"], "sess-1"),
        timeout=2.0,
    )
    assert consumed == 0
    assert proactive._queue.empty()


async def test_web_tile_client_ref_does_not_raise(mind_server_mod):
    """A non-numeric client_ref has nowhere to deliver; it must not blow up.

    Web terminal tiles carry a ``terminal-<uuid>`` client_ref. int() on that
    raises ValueError, which would kill the flush mid-turn.
    """
    session = _make_session(
        [_assistant_text_event("dropped")],
        chat_id="terminal-9f2c1e40-0000-4000-8000-000000000000",
    )
    consumed = await mind_server_mod._flush_unsolicited(
        session, session["proc"], "sess-1"
    )
    assert consumed == 1
    assert proactive._queue.empty()


async def test_missing_stdout_is_a_noop(mind_server_mod):
    """SDK-backed minds expose no subprocess stdout."""
    session = {"proc": None, "chat_id": 1}
    assert await mind_server_mod._flush_unsolicited(session, None, "sess-1") == 0


def test_proactive_chat_id_rejects_non_numeric(mind_server_mod):
    assert mind_server_mod._proactive_chat_id({"chat_id": "555"}) == 555
    assert mind_server_mod._proactive_chat_id({"chat_id": 555}) == 555
    assert mind_server_mod._proactive_chat_id({"chat_id": "terminal-abc"}) is None
    assert mind_server_mod._proactive_chat_id({"chat_id": None}) is None
    assert mind_server_mod._proactive_chat_id({}) is None
