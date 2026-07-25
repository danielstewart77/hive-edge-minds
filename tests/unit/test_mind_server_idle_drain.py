"""Unit tests for the proactive idle-drain path on mind_server.py.

Covers the additive unsolicited-output delivery: while no request is in
flight, the per-session background drain reads harness stdout, filters for
assistant ``text`` blocks, and enqueues them for Telegram delivery. It must
never read concurrently with the live request reader, and must be cancelled
when the session is killed.
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


def _assistant_tool_event() -> bytes:
    return (json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {}},
            {"type": "tool_result", "content": "output"},
        ]},
    }) + "\n").encode()


def _system_event() -> bytes:
    return (json.dumps({"type": "system", "subtype": "init"}) + "\n").encode()


class _FakeStdout:
    """Serves queued byte lines to ``readline``, then blocks (simulates idle)."""

    def __init__(self, lines):
        self._lines = list(lines)
        self._exhausted = asyncio.Event()

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        # Simulate a subprocess that is alive but silent — readline would hang.
        await self._exhausted.wait()
        return b""


def _make_session(lines, in_flight=False, chat_id=555):
    proc = MagicMock()
    proc.stdout = _FakeStdout(lines)
    proc.returncode = None
    return {
        "proc": proc,
        "model": "sonnet",
        "chat_id": chat_id,
        "in_flight": in_flight,
        "stdout_lock": asyncio.Lock(),
        "cancel_event": asyncio.Event(),
    }


async def _drain_briefly(mind_server, session, ticks=0.4):
    task = asyncio.create_task(mind_server._idle_drain(session))
    await asyncio.sleep(ticks)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_unsolicited_assistant_text_is_enqueued(mind_server_mod):
    session = _make_session([_assistant_text_event("proactive hello")], chat_id=777)
    await _drain_briefly(mind_server_mod, session)
    assert not proactive._queue.empty()
    chat_id, text = await proactive.get()
    assert chat_id == 777
    assert text == "proactive hello"


async def test_output_while_in_flight_is_not_enqueued(mind_server_mod):
    session = _make_session([_assistant_text_event("solicited")], in_flight=True)
    await _drain_briefly(mind_server_mod, session)
    assert proactive._queue.empty()


async def test_tool_and_non_assistant_events_never_enqueued(mind_server_mod):
    session = _make_session([
        _assistant_tool_event(),
        _system_event(),
        (json.dumps({"type": "result", "session_id": "abc"}) + "\n").encode(),
    ])
    await _drain_briefly(mind_server_mod, session)
    assert proactive._queue.empty()


async def test_multiple_text_blocks_each_enqueued(mind_server_mod):
    session = _make_session([
        (json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "one"},
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "text", "text": "two"},
            ]},
        }) + "\n").encode(),
    ], chat_id=42)
    await _drain_briefly(mind_server_mod, session)
    got = []
    while not proactive._queue.empty():
        got.append(await proactive.get())
    assert got == [(42, "one"), (42, "two")]


async def test_drain_does_not_read_stdout_while_request_holds_lock(mind_server_mod):
    """The idle drain must not consume stdout while the request reader owns the lock."""
    session = _make_session([_assistant_text_event("should wait")])
    await session["stdout_lock"].acquire()  # simulate live request reader holding lock
    try:
        task = asyncio.create_task(mind_server_mod._idle_drain(session))
        await asyncio.sleep(0.3)
        # Lock held by "request" — drain must not have read/enqueued anything.
        assert proactive._queue.empty()
    finally:
        session["stdout_lock"].release()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Once released, the drain should pick up the line.
    await asyncio.sleep(0.3)


async def test_drain_exits_on_eof(mind_server_mod):
    """When readline returns empty bytes (EOF), the drain task exits on its own."""
    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(return_value=b"")
    proc.returncode = None
    session = {
        "proc": proc,
        "model": "sonnet",
        "chat_id": 1,
        "in_flight": False,
        "stdout_lock": asyncio.Lock(),
        "cancel_event": asyncio.Event(),
    }
    await asyncio.wait_for(mind_server_mod._idle_drain(session), timeout=2.0)


class TestKillCancelsDrain:
    def test_kill_session_cancels_drain_task(self, mind_server_mod):
        """Killing a session cancels its idle-drain task (no orphan)."""
        client = TestClient(mind_server_mod.app, raise_server_exceptions=False)

        async def _never():
            await asyncio.Event().wait()

        # Build a session with a real running drain task.
        loop = asyncio.new_event_loop()
        try:
            drain_task = loop.create_task(_never())
            proc = MagicMock()
            proc.returncode = None
            session = {
                "proc": proc,
                "model": "sonnet",
                "chat_id": 1,
                "drain_task": drain_task,
                "cancel_event": None,
            }
            mind_server_mod._sessions["kill-me"] = session

            with patch.object(mind_server_mod.impl, "kill", new=AsyncMock()):
                resp = client.delete("/sessions/kill-me")

            assert resp.status_code == 200
            # kill_session must have requested cancellation of the drain task.
            assert drain_task.cancelling() > 0 or drain_task.cancelled()
        finally:
            drain_task.cancel()
            # Let the loop process the cancellation so the coroutine is closed.
            try:
                loop.run_until_complete(asyncio.gather(drain_task, return_exceptions=True))
            except RuntimeError:
                pass
            loop.close()
            mind_server_mod._sessions.pop("kill-me", None)
