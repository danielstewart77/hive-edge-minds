"""Unit tests for the Codex harness adapter template.

Mocks the codex subprocess so we can assert the command shape, the system-prompt
folding, the codex-event parsing, and the thread_id lifecycle without spawning a
real `codex exec`.
"""

import json
import os
import subprocess

import pytest

import mind_templates.codex_cli_codex as impl


class FakeStdin:
    def __init__(self):
        self.buffer = b""

    def write(self, data):
        self.buffer += data

    async def drain(self):
        pass

    def close(self):
        pass


class FakeStdout:
    """Async-iterable over a list of raw bytes lines, like proc.stdout."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)


class FakeProc:
    def __init__(self, lines):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(lines)
        self.stderr = FakeStdout([])
        self.returncode = None
        self.pid = 4242

    async def wait(self):
        self.returncode = 0
        return 0


def _ev(obj):
    return (json.dumps(obj) + "\n").encode()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Clear session state between tests."""
    impl._sessions.clear()
    impl.THREADS.clear()
    yield
    impl._sessions.clear()
    impl.THREADS.clear()


def _install_fake_exec(monkeypatch, lines, recorder):
    async def fake_exec(*cmd, **kwargs):
        recorder["cmd"] = list(cmd)
        recorder["kwargs"] = kwargs
        proc = FakeProc(lines)
        recorder["proc"] = proc
        return proc

    monkeypatch.setattr(impl.asyncio, "create_subprocess_exec", fake_exec)


async def _spawn(session_id="s1", resume_sid=None):
    return await impl.spawn(
        session_id=session_id,
        model="gpt-5.4",
        resume_sid=resume_sid,
        system_prompt_blocks="SOUL",
        surface_prompt="SURFACE",
        client_ref="tg:123",
        owner_type="user",
        owner_ref="owner1",
    )


async def _collect(session_id, content):
    return [ev async for ev in impl.send(session_id, content)]


async def test_spawn_initializes_state():
    state = await _spawn()
    assert state["system_prompt"] == "SOUL\n\nSURFACE"
    assert state["thread_id"] is None
    assert state["model"] == "gpt-5.4"
    assert state["client_ref"] == "tg:123"
    assert state["owner_type"] == "user"
    assert state["owner_ref"] == "owner1"
    assert impl._sessions["s1"] is state


async def test_send_new_thread_cmd_stdin_and_events(monkeypatch):
    await _spawn()
    rec = {}
    _install_fake_exec(monkeypatch, [
        _ev({"type": "thread.started", "thread_id": "t1"}),
        _ev({"type": "item.completed",
             "item": {"type": "agent_message", "text": "hello meatbag"}}),
        _ev({"type": "turn.completed"}),
    ], rec)

    events = await _collect("s1", "ping")

    # New thread: no `resume`, prompt folded into stdin.
    assert rec["cmd"] == ["codex", "exec", "--json",
                          "--dangerously-bypass-approvals-and-sandbox",
                          "--dangerously-bypass-hook-trust", "-"]
    assert rec["proc"].stdin.buffer == b"SOUL\n\nSURFACE\n\n---\n\nping"
    # CODEX_HOME and cwd are wired for the subprocess.
    assert "CODEX_HOME" in rec["kwargs"]["env"]
    assert rec["kwargs"]["env"]["CLIENT_REF"] == "tg:123"
    if os.name == "nt":
        # CREATE_NO_WINDOW, never DETACHED_PROCESS: a detached codex has no
        # console, so its console-subsystem children (node/rust, bash/jq/curl
        # hooks) each allocate a visible conhost window on the kid's desktop.
        flags = rec["kwargs"]["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW
        assert not flags & subprocess.DETACHED_PROCESS
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert rec["kwargs"]["start_new_session"] is True

    # thread_id captured for resumption.
    assert impl._sessions["s1"]["thread_id"] == "t1"

    assistant = [e for e in events if e["type"] == "assistant"]
    assert assistant[0]["message"]["content"][0]["text"] == "hello meatbag"
    result = [e for e in events if e["type"] == "result"][-1]
    assert result["is_error"] is False
    assert result["stop_reason"] == "end_turn"
    assert result["session_id"] == "t1"


async def test_gateway_conversation_id_is_not_used_as_a_codex_thread():
    """Codex mints its own thread and adopts none. Planting the gateway's
    conversation id as a thread id made every resume run against a thread that
    never existed."""
    state = await _spawn(resume_sid="gateway-uuid")
    assert state["thread_id"] is None


async def test_a_known_thread_is_rejoined_on_respawn():
    impl.THREADS["s1"] = "codex-thread-7"
    state = await _spawn(resume_sid="gateway-uuid")
    assert state["thread_id"] == "codex-thread-7"


async def test_send_resume_thread_sends_only_user_message(monkeypatch):
    impl.THREADS["s1"] = "t0"
    await _spawn(resume_sid="gateway-uuid")
    rec = {}
    _install_fake_exec(monkeypatch, [
        _ev({"type": "thread.started", "thread_id": "t0"}),
        _ev({"type": "turn.completed"}),
    ], rec)

    await _collect("s1", "again")

    assert rec["cmd"] == ["codex", "exec", "--json",
                          "--dangerously-bypass-approvals-and-sandbox",
                          "--dangerously-bypass-hook-trust",
                          "resume", "t0", "-"]
    # Resumed turn ships only the user message — codex re-hydrates the thread.
    assert rec["proc"].stdin.buffer == b"again"


async def test_turn_failed_resets_thread_id(monkeypatch):
    await _spawn()
    rec = {}
    _install_fake_exec(monkeypatch, [
        _ev({"type": "thread.started", "thread_id": "t1"}),
        _ev({"type": "turn.failed", "error": {"message": "boom"}}),
    ], rec)

    events = await _collect("s1", "ping")

    # Dirty thread cleared so the next turn starts fresh, not one behind.
    assert impl._sessions["s1"]["thread_id"] is None
    assert events[-1]["is_error"] is True


async def test_incomplete_stream_resets_thread_id(monkeypatch):
    await _spawn()
    rec = {}
    _install_fake_exec(monkeypatch, [
        _ev({"type": "thread.started", "thread_id": "t1"}),
        # stream ends with no turn.completed/turn.failed
    ], rec)

    events = await _collect("s1", "ping")

    assert impl._sessions["s1"]["thread_id"] is None
    assert events[-1]["type"] == "result"
    # An abnormal end is an error, and the reason rides an assistant event so
    # the surface shows it instead of dead air.
    assert events[-1]["is_error"] is True
    assert any(e["type"] == "assistant" for e in events)


async def test_send_without_state_yields_error():
    events = [ev async for ev in impl.send("ghost", "hi")]
    assert events == [{"type": "result", "is_error": True}]


async def test_kill_reaps_and_drops_state():
    await _spawn()
    assert "s1" in impl._sessions
    await impl.kill("s1")
    assert "s1" not in impl._sessions


async def test_codex_profile_env_inserts_profile_flag(monkeypatch):
    """CODEX_PROFILE routes turns through a named codex config profile."""
    monkeypatch.setattr(impl, "CODEX_PROFILE", "proxy")
    await _spawn()
    rec = {}
    _install_fake_exec(monkeypatch, [
        _ev({"type": "thread.started", "thread_id": "t1"}),
        _ev({"type": "turn.completed"}),
    ], rec)

    await _collect("s1", "ping")

    assert rec["cmd"][:5] == ["codex", "exec", "--json", "--profile", "proxy"]


class TestExtractAssistantTexts:
    """The shared codex-event → assistant-visible-text filter."""

    def test_agent_message_yields_text(self):
        event = {"type": "item.completed",
                 "item": {"type": "agent_message", "text": "hello meatbag"}}
        assert impl.extract_assistant_texts(event) == ["hello meatbag"]

    def test_empty_agent_message_yields_nothing(self):
        event = {"type": "item.completed",
                 "item": {"type": "agent_message", "text": ""}}
        assert impl.extract_assistant_texts(event) == []

    @pytest.mark.parametrize("event", [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.completed"},
        {"type": "turn.failed", "error": {"message": "boom"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "file_change"}},
        {"type": "item.completed"},
        {"type": "item.completed", "item": None},
        {},
    ])
    def test_non_assistant_events_yield_nothing(self, event):
        assert impl.extract_assistant_texts(event) == []
