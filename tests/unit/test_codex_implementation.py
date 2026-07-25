"""Unit tests for the Codex harness adapter template.

Mocks the codex subprocess so we can assert the command shape, the system-prompt
folding, the codex-event parsing, and the thread_id lifecycle without spawning a
real `codex exec`.
"""

import json
import os
import subprocess
import time
from unittest.mock import MagicMock, patch

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


class _TmuxRecorder:
    """Stands in for the tmux CLI, recording the argv of every call."""

    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, *args, env=None):
        self.calls.append(list(args))
        self.envs.append(env or {})
        return subprocess.CompletedProcess(
            args=list(args), returncode=self.returncode, stdout="", stderr=self.stderr
        )

    @property
    def new_session(self) -> list[str]:
        for call in self.calls:
            if "new-session" in call:
                return call
        raise AssertionError(f"no new-session in {self.calls}")

    @property
    def pane_argv(self) -> list[str]:
        """What tmux was told to run in the pane — everything after `--`."""
        call = self.new_session
        return call[call.index("--") + 1:]


def _spawned(alive: bool = False, tmux: "_TmuxRecorder | None" = None, **kwargs):
    """Run spawn_pty with tmux, the pty, and the capture thread faked out."""
    tmux = tmux or _TmuxRecorder()
    call_kwargs = {"session_id": "s1", "model": "gpt-5.4", "resume_sid": "conv-1"}
    call_kwargs.update(kwargs)
    with patch("mind_templates.codex_cli_codex._tmux", tmux), \
         patch("mind_templates.codex_cli_codex.pty_session_alive", return_value=alive), \
         patch("mind_templates.codex_cli_codex.pty.openpty", return_value=(11, 22)), \
         patch("mind_templates.codex_cli_codex.fcntl.ioctl"), \
         patch("mind_templates.codex_cli_codex.os.close"), \
         patch("mind_templates.codex_cli_codex.threading.Thread") as thread_cls, \
         patch("mind_templates.codex_cli_codex.subprocess.Popen") as popen:
        popen.return_value = MagicMock(pid=999)
        result = impl.spawn_pty(**call_kwargs)
    return tmux, popen, thread_cls, result


class TestSpawnPty:
    def test_pane_argv_starts_bare_codex_with_update_check_disabled(self):
        """The update-nag screen's Enter default runs `npm install -g` and
        kills codex when that fails inside the container (no write access
        to the global npm dir) — check_for_update_on_startup=false is the
        real fix, not a "don't press Enter" workaround."""
        tmux, _, _, _ = _spawned()

        cmd = tmux.pane_argv
        assert cmd[0] == "codex"
        assert "resume" not in cmd
        assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "check_for_update_on_startup=false"
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--dangerously-bypass-hook-trust" in cmd

    def test_refuses_to_spawn_without_a_conversation_id(self):
        with pytest.raises(ValueError, match="no conversation id"):
            impl.spawn_pty(session_id="s1", model="gpt-5.4")

    def test_known_thread_is_resumed_by_id(self):
        impl.THREADS["s1"] = "thread-abc"
        tmux, _, _, _ = _spawned(session_id="s1")

        cmd = tmux.pane_argv
        assert cmd[:2] == ["codex", "resume"]
        assert cmd[2] == "thread-abc"

    def test_codex_profile_is_wired_when_set(self, monkeypatch):
        monkeypatch.setattr(impl, "CODEX_PROFILE", "proxy")
        tmux, _, _, _ = _spawned()

        cmd = tmux.pane_argv
        assert cmd[cmd.index("--profile") + 1] == "proxy"

    def test_session_is_named_for_the_hive_session(self):
        tmux, popen, _, _ = _spawned(session_id="sess-abc")

        call = tmux.new_session
        assert call[call.index("-s") + 1] == impl.tmux_session_name("sess-abc")
        assert popen.call_args.args[0][-1] == impl.tmux_session_name("sess-abc")

    def test_geometry_is_handed_to_tmux_at_creation(self):
        tmux, _, _, _ = _spawned(cols=132, rows=43)

        call = tmux.new_session
        assert call[call.index("-x") + 1] == "132"
        assert call[call.index("-y") + 1] == "43"

    def test_pane_is_stamped_as_the_terminal_surface(self):
        tmux, popen, _, _ = _spawned()

        assert "HIVE_SURFACE=terminal" in tmux.new_session
        assert popen.call_args.kwargs["env"]["HIVE_SURFACE"] == "terminal"

    def test_pane_gets_codex_home(self):
        tmux, popen, _, _ = _spawned()

        assert any(c.startswith("CODEX_HOME=") for c in tmux.new_session)
        assert popen.call_args.kwargs["env"]["CODEX_HOME"] == str(impl.CODEX_HOME)

    def test_an_existing_terminal_is_attached_not_restarted(self):
        """The bug this forecloses: a second `codex` on one conversation."""
        tmux, popen, thread_cls, _ = _spawned(alive=True)

        assert not any("new-session" in call for call in tmux.calls)
        assert popen.call_args.args[0][:5] == [
            "tmux", "-L", impl.TMUX_SOCKET, "attach-session", "-d"
        ]
        thread_cls.assert_not_called()

    def test_attach_detaches_whoever_held_the_session(self):
        _, popen, _, _ = _spawned(alive=True)
        assert "-d" in popen.call_args.args[0]

    def test_client_takes_the_pty_as_its_controlling_terminal(self):
        _, popen, _, _ = _spawned()
        assert popen.call_args.kwargs["preexec_fn"] is impl._take_controlling_tty

    def test_client_runs_as_a_browser_terminal(self):
        _, popen, _, _ = _spawned()
        assert popen.call_args.kwargs["env"]["TERM"] == "xterm-256color"

    def test_tmux_refusing_to_start_is_raised_not_swallowed(self):
        tmux = _TmuxRecorder(returncode=1, stderr="no server running")
        with pytest.raises(RuntimeError, match="no server running"):
            _spawned(tmux=tmux)

    def test_returns_process_and_master_fd(self):
        _, popen, _, result = _spawned()
        proc, master_fd = result
        assert proc is popen.return_value
        assert master_fd == 11

    def test_pty_wired_as_stdio_and_slave_closed_after_spawn(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.codex_cli_codex._tmux", tmux), \
             patch("mind_templates.codex_cli_codex.pty_session_alive", return_value=True), \
             patch("mind_templates.codex_cli_codex.pty.openpty", return_value=(11, 22)), \
             patch("mind_templates.codex_cli_codex.fcntl.ioctl"), \
             patch("mind_templates.codex_cli_codex.os.close") as mock_close, \
             patch("mind_templates.codex_cli_codex.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=999)
            impl.spawn_pty(session_id="s1", model="gpt-5.4", resume_sid="conv-1")

        kwargs = popen.call_args.kwargs
        assert kwargs["stdin"] == 22 and kwargs["stdout"] == 22 and kwargs["stderr"] == 22
        mock_close.assert_called_once_with(22)

    def test_initial_winsize_set_on_slave_before_spawn(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.codex_cli_codex._tmux", tmux), \
             patch("mind_templates.codex_cli_codex.pty_session_alive", return_value=True), \
             patch("mind_templates.codex_cli_codex.pty.openpty", return_value=(11, 22)), \
             patch("mind_templates.codex_cli_codex.fcntl.ioctl") as mock_ioctl, \
             patch("mind_templates.codex_cli_codex.os.close"), \
             patch("mind_templates.codex_cli_codex.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=999)
            impl.spawn_pty(session_id="s1", model="gpt-5.4",
                            resume_sid="conv-1", cols=132, rows=43)

        assert mock_ioctl.call_args.args[0] == 22

    def test_fresh_terminal_starts_a_thread_capture_watcher(self):
        """No thread yet: a background watcher has to bind whatever codex
        mints once the operator's first message lands, or every later turn
        forks a new thread instead of resuming this terminal's."""
        tmux, _, thread_cls, _ = _spawned(session_id="s2")

        thread_cls.assert_called_once()
        assert thread_cls.call_args.kwargs["target"] is impl._capture_new_thread
        assert thread_cls.call_args.kwargs["args"][0] == "s2"
        assert thread_cls.call_args.kwargs["daemon"] is True

    def test_known_thread_spawn_skips_the_capture_watcher(self):
        """Nothing new to capture — resuming a thread we already know."""
        impl.THREADS["s3"] = "thread-known"
        _, _, thread_cls, _ = _spawned(session_id="s3")

        thread_cls.assert_not_called()


class TestTmuxSessionLifecycle:
    def test_session_name_is_derived_from_the_session_id(self):
        assert impl.tmux_session_name("abc-123") == "MIND_NAME-abc-123"

    def test_alive_follows_has_session(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.codex_cli_codex._tmux", tmux):
            assert impl.pty_session_alive("s1") is True
        assert tmux.calls[0][:2] == ["has-session", "-t"]

        with patch("mind_templates.codex_cli_codex._tmux", _TmuxRecorder(returncode=1)):
            assert impl.pty_session_alive("s1") is False

    def test_kill_ends_the_named_session(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.codex_cli_codex._tmux", tmux):
            assert impl.kill_pty_session("s1") is True
        assert tmux.calls[0] == ["kill-session", "-t", "MIND_NAME-s1"]

    def test_kill_reports_when_there_was_nothing_to_kill(self):
        with patch("mind_templates.codex_cli_codex._tmux", _TmuxRecorder(returncode=1)):
            assert impl.kill_pty_session("s1") is False


class TestCaptureNewThread:
    """The watcher that binds a fresh interactive session's minted thread.

    A bare `codex` TUI reports no thread id up front — unlike `codex exec`,
    the rollout file only appears once the operator sends a real message —
    so the watcher polls for as long as the terminal is alive rather than a
    short fixed timeout.
    """

    def _rollout(self, tmp_path, thread_id, mtime):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        path = sessions_dir / f"rollout-2026-07-25T19-00-00-{thread_id}.jsonl"
        path.write_text("{}")
        os.utime(path, (mtime, mtime))
        return path

    def test_binds_the_thread_once_its_rollout_appears(self, tmp_path, monkeypatch):
        monkeypatch.setattr(impl, "CODEX_HOME", tmp_path)
        spawned_at = time.time()
        self._rollout(tmp_path, "019f9aaa-7f0f-71a0-af10-c45007904da0", spawned_at + 1)

        with patch("mind_templates.codex_cli_codex.pty_session_alive", return_value=True):
            impl._capture_new_thread("s1", spawned_at, max_wait=2.0)

        assert impl.THREADS["s1"] == "019f9aaa-7f0f-71a0-af10-c45007904da0"

    def test_ignores_rollouts_older_than_the_spawn(self, tmp_path, monkeypatch):
        """A stale rollout from a previous terminal on this session must not
        be mistaken for the one this spawn is waiting on."""
        monkeypatch.setattr(impl, "CODEX_HOME", tmp_path)
        spawned_at = time.time()
        self._rollout(tmp_path, "00000000-0000-0000-0000-000000000000", spawned_at - 100)

        alive = iter([True, False])
        with patch("mind_templates.codex_cli_codex.pty_session_alive",
                   side_effect=lambda sid: next(alive, False)), \
             patch("mind_templates.codex_cli_codex.time.sleep"):
            impl._capture_new_thread("s1", spawned_at, max_wait=30.0)

        assert "s1" not in impl.THREADS

    def test_gives_up_once_the_terminal_dies(self, tmp_path, monkeypatch):
        """No rollout ever appears if the operator never sent a message —
        the watcher must stop with the terminal, not spin for max_wait."""
        monkeypatch.setattr(impl, "CODEX_HOME", tmp_path)
        started = time.time()

        with patch("mind_templates.codex_cli_codex.pty_session_alive", return_value=False):
            impl._capture_new_thread("s1", started, max_wait=30.0)

        assert time.time() - started < 5.0
        assert "s1" not in impl.THREADS


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
