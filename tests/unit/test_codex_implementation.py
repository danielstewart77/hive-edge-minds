"""Unit tests for the Codex harness adapter template.

Mocks the codex subprocess so we can assert the command shape, the system-prompt
folding, the codex-event parsing, and the thread_id lifecycle without spawning a
real `codex exec`.
"""

import json
import os
import subprocess
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
def _isolate(monkeypatch, tmp_path):
    """Clear session state between tests."""
    monkeypatch.setattr(impl, "_THREAD_MAP_PATH", tmp_path / "thread-map.json")
    monkeypatch.setattr(impl, "CODEX_PROFILE", "")
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
    report_thread = MagicMock()
    with patch("mind_templates.codex_cli_codex._tmux", tmux), \
         patch("mind_templates.codex_cli_codex.pty_session_alive", return_value=alive), \
         patch("mind_templates.codex_cli_codex.pty.openpty", return_value=(11, 22)), \
         patch("mind_templates.codex_cli_codex.fcntl.ioctl"), \
         patch("mind_templates.codex_cli_codex.os.close"), \
         patch("mind_templates.codex_cli_codex._create_terminal_thread",
               return_value="thread-created") as create_thread, \
         patch("mind_templates.codex_cli_codex._report_thread", report_thread), \
         patch("mind_templates.codex_cli_codex.subprocess.Popen") as popen:
        popen.return_value = MagicMock(pid=999)
        result = impl.spawn_pty(**call_kwargs)
    create_thread.report_thread = report_thread
    return tmux, popen, create_thread, result


class TestSpawnPty:
    def test_pane_argv_resumes_precreated_thread_with_update_check_disabled(self):
        """The update-nag screen's Enter default runs `npm install -g` and
        kills codex when that fails inside the container (no write access
        to the global npm dir) — check_for_update_on_startup=false is the
        real fix, not a "don't press Enter" workaround."""
        tmux, _, _, _ = _spawned()

        cmd = tmux.pane_argv
        assert cmd[0] == "codex"
        assert cmd[:3] == ["codex", "resume", "thread-created"]
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

    def test_fresh_terminal_precreates_and_reports_exact_thread(self):
        tmux, _, create_thread, _ = _spawned(session_id="s2")

        create_thread.assert_called_once_with("gpt-5.4")
        create_thread.report_thread.assert_called_once_with("s2", "thread-created")
        assert tmux.pane_argv[:3] == ["codex", "resume", "thread-created"]

    def test_gateway_thread_is_resumed_without_creating_another(self):
        impl.THREADS["s3"] = "thread-known"
        tmux, _, create_thread, _ = _spawned(
            session_id="s3", harness_sid="thread-gateway"
        )

        create_thread.assert_not_called()
        assert tmux.pane_argv[:3] == ["codex", "resume", "thread-gateway"]

    def test_local_safety_copy_backfills_the_gateway(self):
        impl.THREADS["s4"] = "thread-local"
        _, _, create_thread, _ = _spawned(session_id="s4")

        create_thread.assert_not_called()
        create_thread.report_thread.assert_called_once_with("s4", "thread-local")


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


class TestThreadMap:
    def test_remembered_thread_survives_memory_reset(self):
        impl._remember_thread("s1", "thread-one")
        impl.THREADS.clear()
        assert impl._load_thread_map() == {"s1": "thread-one"}

    def test_forget_removes_persisted_mapping(self):
        impl._remember_thread("s1", "thread-one")
        impl._forget_thread("s1")
        assert impl._load_thread_map() == {}


class TestCreateTerminalThread:
    def test_app_server_returns_exact_empty_thread_id(self, monkeypatch):
        class Input:
            def __init__(self):
                self.messages = []

            def write(self, value):
                self.messages.append(json.loads(value))

            def flush(self):
                pass

        class Output:
            def __init__(self):
                self.lines = iter([
                    json.dumps({"id": 1, "result": {}}) + "\n",
                    json.dumps({"method": "thread/started", "params": {}}) + "\n",
                    json.dumps({"id": 2, "result": {
                        "thread": {"id": "thread-exact"}
                    }}) + "\n",
                ])

            def readline(self):
                return next(self.lines, "")

        proc = MagicMock()
        proc.stdin = Input()
        proc.stdout = Output()
        proc.poll.return_value = None
        monkeypatch.setattr(impl.subprocess, "Popen", MagicMock(return_value=proc))
        monkeypatch.setattr(impl.select, "select", lambda *args: ([proc.stdout], [], []))

        assert impl._create_terminal_thread("gpt-5.4") == "thread-exact"
        assert proc.stdin.messages[2]["method"] == "thread/start"
        assert proc.stdin.messages[2]["params"]["model"] == "gpt-5.4"
        assert "input" not in proc.stdin.messages[2]["params"]
        proc.terminate.assert_called_once()


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
