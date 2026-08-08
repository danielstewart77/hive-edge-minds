"""Unit tests for the Codex harness adapter template.

Mocks the codex subprocess so we can assert the command shape, the system-prompt
folding, the codex-event parsing, and the thread_id lifecycle without spawning a
real `codex exec`.
"""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import mind_templates.codex_cli as impl


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


class TestChatTurnEnv:
    """Codex is per-turn, so what spawn() resolves has to reach send()'s
    subprocess env — that env is what the UserPromptSubmit hooks read.
    """

    @staticmethod
    def _turn_env(monkeypatch, **spawn_kwargs):
        async def _go():
            call = {
                "session_id": "row-9",
                "model": "gpt-5.4",
                "resume_sid": None,
                "system_prompt_blocks": "SOUL",
                "client_ref": "tg:123",
            }
            call.update(spawn_kwargs)
            await impl.spawn(**call)
            rec = {}
            _install_fake_exec(monkeypatch, [_ev({"type": "turn.completed"})], rec)
            [ev async for ev in impl.send(call["session_id"], "ping")]
            return rec["kwargs"]["env"]

        # This suite may itself run inside a surface-stamped process; send()
        # copies os.environ, so mask the inherited value.
        monkeypatch.delenv("HIVE_SURFACE", raising=False)
        return asyncio.run(_go())

    def test_surface_from_the_gateway_reaches_the_turn(self, monkeypatch):
        env = self._turn_env(monkeypatch, surface="telegram",
                             owner_type="telegram:uuid")
        assert env["HIVE_SURFACE"] == "telegram"

    def test_missing_surface_falls_back_to_the_owner_type_prefix(self, monkeypatch):
        env = self._turn_env(monkeypatch, owner_type="discord:uuid")
        assert env["HIVE_SURFACE"] == "discord"

    def test_no_surface_and_no_owner_type_sets_nothing(self, monkeypatch):
        env = self._turn_env(monkeypatch)
        assert "HIVE_SURFACE" not in env

    def test_gateway_session_id_reaches_the_turn(self, monkeypatch):
        env = self._turn_env(monkeypatch)
        assert env["HIVE_SESSION_ID"] == "row-9"


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
    with patch("mind_templates.codex_cli._tmux", tmux), \
         patch("mind_templates.codex_cli.pty_session_alive", return_value=alive), \
         patch("mind_templates.codex_cli.pty.openpty", return_value=(11, 22)), \
         patch("mind_templates.codex_cli.fcntl.ioctl"), \
         patch("mind_templates.codex_cli.os.close"), \
         patch("mind_templates.codex_cli._rollout_exists", return_value=True), \
         patch("mind_templates.codex_cli._watch_for_new_thread_in_background") as watcher, \
         patch("mind_templates.codex_cli._report_thread", report_thread), \
         patch("mind_templates.codex_cli.subprocess.Popen") as popen:
        popen.return_value = MagicMock(pid=999)
        result = impl.spawn_pty(**call_kwargs)
    watcher.report_thread = report_thread
    return tmux, popen, watcher, result


class TestSpawnPty:
    def test_fresh_pane_argv_is_bare_codex_with_update_check_disabled(self):
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

    def test_a_fresh_spawn_carries_the_seed_all_the_way_into_the_pane(
        self, monkeypatch, tmp_path
    ):
        """`mind_server` proves the seed reaches `spawn_pty`'s argument; this
        proves the adapter does something with it. Codex has no
        system-prompt flag, so the seed rides in as the positional opening
        turn — dropping it between the two would leave a rotated
        conversation opening bare, with nothing failing.
        """
        monkeypatch.setattr(impl, "CODEX_HOME", tmp_path)
        tmux, _, _, _ = _spawned(system_prompt="<soul>carried forward</soul>")

        cmd = tmux.pane_argv
        assert cmd[:2] == ["/bin/sh", "-c"]
        seed_file = tmp_path / "rotation-seeds" / "s1.txt"
        assert seed_file.read_text() == "<soul>carried forward</soul>"
        assert str(seed_file) in cmd[2]
        # And it is not left lying around world-readable.
        assert seed_file.stat().st_mode & 0o077 == 0

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

    def test_pane_gets_the_gateway_session_id(self):
        """The end-session skill closes the session row it runs inside, so the
        row's id has to be on the harness process. CODEX_THREAD_ID names the
        codex conversation, not the row."""
        tmux, popen, _, _ = _spawned(session_id="row-77")

        assert "HIVE_SESSION_ID=row-77" in tmux.new_session
        assert popen.call_args.kwargs["env"]["HIVE_SESSION_ID"] == "row-77"

    def test_pane_gets_codex_home(self):
        tmux, popen, _, _ = _spawned()

        assert any(c.startswith("CODEX_HOME=") for c in tmux.new_session)
        assert popen.call_args.kwargs["env"]["CODEX_HOME"] == str(impl.CODEX_HOME)

    def test_pane_carries_the_session_env_the_hooks_need(self):
        """Without CLIENT_REF in the pane, the Stop hook's rotation check
        bails on every fire and a codex terminal never rotates at all."""
        tmux, popen, _, _ = _spawned(
            client_ref="term-1", owner_type="terminal", owner_ref="tile-9"
        )

        call = tmux.new_session
        assert "CLIENT_REF=term-1" in call
        assert "OWNER_TYPE=terminal" in call
        assert "OWNER_REF=tile-9" in call
        assert popen.call_args.kwargs["env"]["CLIENT_REF"] == "term-1"

    def test_absent_session_env_is_left_unset_rather_than_stamped_empty(self):
        tmux, _, _, _ = _spawned()

        assert not any(c.startswith("CLIENT_REF=") for c in tmux.new_session)
        assert not any(c.startswith("OWNER_TYPE=") for c in tmux.new_session)
        assert not any(c.startswith("OWNER_REF=") for c in tmux.new_session)

    def test_an_existing_terminal_is_attached_not_restarted(self):
        """The bug this forecloses: a second `codex` on one conversation."""
        tmux, popen, watcher, _ = _spawned(alive=True)

        assert not any("new-session" in call for call in tmux.calls)
        assert popen.call_args.args[0][:5] == [
            "tmux", "-L", impl.TMUX_SOCKET, "attach-session", "-d"
        ]
        watcher.assert_not_called()

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
        with patch("mind_templates.codex_cli._tmux", tmux), \
             patch("mind_templates.codex_cli.pty_session_alive", return_value=True), \
             patch("mind_templates.codex_cli.pty.openpty", return_value=(11, 22)), \
             patch("mind_templates.codex_cli.fcntl.ioctl"), \
             patch("mind_templates.codex_cli.os.close") as mock_close, \
             patch("mind_templates.codex_cli.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=999)
            impl.spawn_pty(session_id="s1", model="gpt-5.4", resume_sid="conv-1")

        kwargs = popen.call_args.kwargs
        assert kwargs["stdin"] == 22 and kwargs["stdout"] == 22 and kwargs["stderr"] == 22
        mock_close.assert_called_once_with(22)

    def test_initial_winsize_set_on_slave_before_spawn(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.codex_cli._tmux", tmux), \
             patch("mind_templates.codex_cli.pty_session_alive", return_value=True), \
             patch("mind_templates.codex_cli.pty.openpty", return_value=(11, 22)), \
             patch("mind_templates.codex_cli.fcntl.ioctl") as mock_ioctl, \
             patch("mind_templates.codex_cli.os.close"), \
             patch("mind_templates.codex_cli.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=999)
            impl.spawn_pty(session_id="s1", model="gpt-5.4",
                            resume_sid="conv-1", cols=132, rows=43)

        assert mock_ioctl.call_args.args[0] == 22

    def test_fresh_terminal_starts_bare_and_watches_for_the_thread_codex_mints(self):
        """A thread id cannot be pre-minted: app-server hands one back without
        writing its rollout, so `codex resume` on it dies. The pane starts
        bare and a watcher reports the id codex writes on the first turn."""
        impl.THREADS.pop("s2", None)
        tmux, _, watcher, _ = _spawned(session_id="s2")

        assert tmux.pane_argv[0] == "codex" and "resume" not in tmux.pane_argv
        assert watcher.call_args.args[0] == "s2"
        watcher.report_thread.assert_not_called()

    def test_gateway_thread_is_resumed_without_watching_for_another(self):
        impl.THREADS["s3"] = "thread-known"
        tmux, _, watcher, _ = _spawned(
            session_id="s3", harness_sid="thread-gateway"
        )

        watcher.assert_not_called()
        assert tmux.pane_argv[:3] == ["codex", "resume", "thread-gateway"]

    def test_local_safety_copy_backfills_the_gateway(self):
        impl.THREADS["s4"] = "thread-local"
        _, _, watcher, _ = _spawned(session_id="s4")

        watcher.assert_not_called()
        watcher.report_thread.assert_called_once_with("s4", "thread-local")


class TestRotatePtySession:
    """A rotation replaces the conversation; the session, the pane and its
    client all stay. The session id is permanent — it is what the gateway,
    the tile and the turn ledger are keyed to — so nothing is renamed."""

    def _rotate(self, tmux=None, alive=True, seed_home=None, **kwargs):
        tmux = tmux or _TmuxRecorder()
        call_kwargs = {"session_id": "s1", "new_claude_sid": "conv-2",
                       "system_prompt": "carry-forward"}
        call_kwargs.update(kwargs)
        with patch("mind_templates.codex_cli.CODEX_HOME",
                   seed_home or Path(tempfile.mkdtemp())), \
             patch("mind_templates.codex_cli._tmux", tmux), \
             patch("mind_templates.codex_cli.pty_session_alive", return_value=alive), \
             patch("mind_templates.codex_cli._existing_rollout_paths", return_value=set()), \
             patch("mind_templates.codex_cli._watch_for_new_thread_in_background") as watcher:
            rotated = impl.rotate_pty_session(**call_kwargs)
        return tmux, watcher, rotated

    def test_the_pane_is_respawned_in_the_session_that_already_exists(self):
        tmux, _, rotated = self._rotate()

        assert rotated is True
        respawn = next(c for c in tmux.calls if c[0] == "respawn-pane")
        assert "-k" in respawn and respawn[respawn.index("-t") + 1] == "MIND_NAME-s1"
        # Nothing renames the session and nothing starts a second one: both
        # move the conversation out from under a tile that is holding it.
        assert not any(c[0] in ("rename-session", "new-session") for c in tmux.calls)

    def test_the_carry_forward_rides_in_as_the_opening_prompt(self, tmp_path):
        """Codex has no --append-system-prompt, so the seed goes where the
        per-turn path puts it: at the head of the conversation. It travels in
        a file — a real carry-forward is far past the length tmux will accept
        in a respawn-pane command."""
        tmux, _, _ = self._rotate(
            seed_home=tmp_path, system_prompt="carry-forward " * 4000
        )

        respawn = next(c for c in tmux.calls if c[0] == "respawn-pane")
        argv = respawn[respawn.index("--") + 1:]
        assert argv[0] == "/bin/sh"
        assert len(" ".join(argv)) < 1000, "the seed is still in the tmux command"
        seed_file = tmp_path / "rotation-seeds" / "s1.txt"
        assert seed_file.read_text() == "carry-forward " * 4000
        # Bare codex, no resume: rotation starts a fresh thread and the seed
        # is its opening turn.
        # An unreadable seed falls through to an unseeded codex rather than
        # exec'ing an empty prompt, so the seeded exec is not the last clause.
        assert 'exec codex ' in argv[-1]
        assert 'check_for_update_on_startup=false "$seed"' in argv[-1]
        assert "resume" not in argv[-1]

    def test_the_new_thread_is_watched_for_under_the_same_session(self):
        tmux, watcher, _ = self._rotate()
        assert watcher.call_args.args[0] == "s1"

    def test_the_replaced_thread_is_forgotten(self):
        """The old codex thread belongs to the conversation being retired. A
        later reattach that resumed it would undo the rotation."""
        impl.THREADS["s1"] = "thread-old"
        self._rotate()
        assert "s1" not in impl.THREADS

    def test_the_respawned_pane_can_still_arm_the_next_rotation(self):
        """respawn-pane builds the pane's env from the tmux server's, which
        never had these. Dropping them rotates once and then never again."""
        tmux, _, _ = self._rotate(
            client_ref="term-1", owner_type="terminal", owner_ref="tile-9"
        )

        respawn = next(c for c in tmux.calls if c[0] == "respawn-pane")
        assert "CLIENT_REF=term-1" in respawn
        assert "OWNER_TYPE=terminal" in respawn
        assert "OWNER_REF=tile-9" in respawn
        assert "HIVE_SURFACE=terminal" in respawn

    def test_no_live_terminal_declines(self):
        tmux, watcher, rotated = self._rotate(alive=False)

        assert rotated is False
        assert tmux.calls == []
        watcher.assert_not_called()

    def test_tmux_refusing_the_respawn_is_raised_not_swallowed(self):
        with pytest.raises(RuntimeError, match="no server running"):
            self._rotate(tmux=_TmuxRecorder(returncode=1, stderr="no server running"))


class TestTmuxSessionLifecycle:
    def test_session_name_is_derived_from_the_session_id(self):
        assert impl.tmux_session_name("abc-123") == "MIND_NAME-abc-123"

    def test_alive_follows_has_session(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.codex_cli._tmux", tmux):
            assert impl.pty_session_alive("s1") is True
        assert tmux.calls[0][:2] == ["has-session", "-t"]

        with patch("mind_templates.codex_cli._tmux", _TmuxRecorder(returncode=1)):
            assert impl.pty_session_alive("s1") is False

    def test_kill_ends_the_named_session(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.codex_cli._tmux", tmux):
            assert impl.kill_pty_session("s1") is True
        assert tmux.calls[0] == ["kill-session", "-t", "=MIND_NAME-s1"]

    def test_kill_reports_when_there_was_nothing_to_kill(self):
        with patch("mind_templates.codex_cli._tmux", _TmuxRecorder(returncode=1)):
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


class TestReportThreadAuth:
    """_report_thread authenticates with the canonical comms bearer token."""

    def _capture(self, monkeypatch):
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["auth"] = request.headers.get("Authorization")
            return FakeResponse()

        monkeypatch.setenv("COMMS_URL", "http://127.0.0.1:8426")
        monkeypatch.setattr(impl.urllib.request, "urlopen", fake_urlopen)
        return captured

    def test_prefers_comms_bearer_token(self, monkeypatch):
        captured = self._capture(monkeypatch)
        monkeypatch.setenv("COMMS_BEARER_TOKEN", "canonical")
        monkeypatch.setenv("HIVEMIND_BROKER_TOKEN", "legacy")
        impl._report_thread("sess-1", "thread-1")
        assert captured["auth"] == "Bearer canonical"

    def test_falls_back_to_legacy_alias(self, monkeypatch):
        captured = self._capture(monkeypatch)
        monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("HIVEMIND_BROKER_TOKEN", "legacy")
        impl._report_thread("sess-1", "thread-1")
        assert captured["auth"] == "Bearer legacy"

    def test_no_token_sends_no_auth_header(self, monkeypatch):
        captured = self._capture(monkeypatch)
        monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("HIVEMIND_BROKER_TOKEN", raising=False)
        impl._report_thread("sess-1", "thread-1")
        assert captured["auth"] is None
