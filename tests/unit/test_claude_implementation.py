"""Tests for mind_templates/claude_cli.py::spawn_pty.

The web terminal's `claude` runs inside a tmux session named for the hive
session; a browser attach is a tmux client in a pty. So `spawn_pty` has two
halves worth pinning down: what gets created the first time (the claude argv,
the pane environment, the session name) and what happens every other time
(attach only — never a second `claude` on one conversation).

The mind never mints a conversation id. The gateway mints one when the
session is created and hands it down on every spawn, so `spawn_pty` either
resumes a transcript that exists or declares the id it was given, and
refuses outright to run without one.
"""
import asyncio
import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mind_templates.claude_cli as implementation


def _fake_registry(env_overrides=None):
    provider = MagicMock()
    provider.env_overrides = env_overrides or {}
    registry = MagicMock()
    registry.get_provider.return_value = provider
    return registry


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


def _spawned(alive: bool = False, tmux: _TmuxRecorder | None = None, **kwargs):
    """Run spawn_pty with tmux and the pty faked out; return (tmux, popen)."""
    tmux = tmux or _TmuxRecorder()
    call_kwargs = {"session_id": "s1", "model": "sonnet", "resume_sid": "conv-1"}
    call_kwargs.update(kwargs)
    with patch("mind_templates.claude_cli._tmux", tmux), \
         patch("mind_templates.claude_cli.pty_session_alive", return_value=alive), \
         patch("mind_templates.claude_cli.pty.openpty", return_value=(11, 22)), \
         patch("mind_templates.claude_cli.fcntl.ioctl"), \
         patch("mind_templates.claude_cli.os.close"), \
         patch("mind_templates.claude_cli.subprocess.Popen") as popen:
        popen.return_value = MagicMock(pid=999)
        result = implementation.spawn_pty(**call_kwargs)
    return tmux, popen, result


class TestSpawnPty:
    def test_pane_argv_omits_print_mode_flags(self):
        tmux, _, _ = _spawned()

        cmd = tmux.pane_argv
        assert cmd[0] == "claude"
        assert "-p" not in cmd
        assert "--input-format" not in cmd
        assert "--output-format" not in cmd
        assert "--append-system-prompt" not in cmd
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "sonnet[1m]"

    def test_refuses_to_spawn_without_a_conversation_id(self):
        """A mind that invents an id creates a conversation only it knows
        about — the blank-terminal bug. Fail loudly instead."""
        with pytest.raises(ValueError, match="no conversation id"):
            implementation.spawn_pty(session_id="s1", model="sonnet")

    def test_mcp_config_is_wired_when_given(self):
        with patch("mind_templates.claude_cli._claude_transcript_exists", return_value=True):
            tmux, _, _ = _spawned(model="opus", resume_sid="sid-123", mcp_config="/opt/mcp.json")

        cmd = tmux.pane_argv
        assert cmd[cmd.index("--resume") + 1] == "sid-123"
        assert cmd[cmd.index("--mcp-config") + 1] == "/opt/mcp.json"

    def test_mcp_config_omitted_when_absent(self):
        tmux, _, _ = _spawned()
        assert "--mcp-config" not in tmux.pane_argv

    def test_conversation_without_a_transcript_is_declared_not_resumed(self):
        """The session's first process. The id is still the gateway's — it
        is being pinned for the first time, not invented."""
        with patch("mind_templates.claude_cli._claude_transcript_exists", return_value=False):
            tmux, _, _ = _spawned(resume_sid="sid-ghost")

        cmd = tmux.pane_argv
        assert "--resume" not in cmd
        assert cmd[cmd.index("--session-id") + 1] == "sid-ghost"

    def test_conversation_with_a_transcript_is_resumed_in_place(self):
        with patch("mind_templates.claude_cli._claude_transcript_exists", return_value=True):
            tmux, _, _ = _spawned(resume_sid="sid-real")

        cmd = tmux.pane_argv
        assert cmd[cmd.index("--resume") + 1] == "sid-real"
        assert "--session-id" not in cmd

    def test_session_is_named_for_the_hive_session(self):
        tmux, popen, _ = _spawned(session_id="sess-abc")

        call = tmux.new_session
        assert call[call.index("-s") + 1] == implementation.tmux_session_name("sess-abc")
        # ...and the client attaches to that same name, not a fresh one.
        assert popen.call_args.args[0][-1] == implementation.tmux_session_name("sess-abc")

    def test_geometry_is_handed_to_tmux_at_creation(self):
        tmux, _, _ = _spawned(cols=132, rows=43)

        call = tmux.new_session
        assert call[call.index("-x") + 1] == "132"
        assert call[call.index("-y") + 1] == "43"

    def test_provider_env_overrides_reach_the_pane(self):
        # The tmux server is long-lived and predates this spawn, so anything
        # per-spawn has to ride on `-e` or the pane never sees it.
        registry = _fake_registry(env_overrides={"ANTHROPIC_BASE_URL": "http://example.test"})
        tmux, popen, _ = _spawned(registry=registry)

        assert "ANTHROPIC_BASE_URL=http://example.test" in tmux.new_session
        assert popen.call_args.kwargs["env"]["ANTHROPIC_BASE_URL"] == "http://example.test"

    def test_pane_is_stamped_as_the_terminal_surface(self):
        """A pty spawn is the web terminal by definition. The pane env carries
        HIVE_SURFACE=terminal so the surface_inject hook can tell the model
        which surface its turns arrive on."""
        tmux, popen, _ = _spawned()

        assert "HIVE_SURFACE=terminal" in tmux.new_session
        assert popen.call_args.kwargs["env"]["HIVE_SURFACE"] == "terminal"

    def test_rotation_env_reaches_the_pane(self):
        """The rotation Stop hook reads CLIENT_REF/OWNER_TYPE/OWNER_REF from
        the process env. Without them on a terminal spawn the hook bails on
        every fire and a terminal conversation never rotates."""
        tmux, popen, _ = _spawned(client_ref="terminal-abc", owner_type="web", owner_ref="u1")

        assert "CLIENT_REF=terminal-abc" in tmux.new_session
        assert "OWNER_TYPE=web" in tmux.new_session
        assert "OWNER_REF=u1" in tmux.new_session
        assert popen.call_args.kwargs["env"]["CLIENT_REF"] == "terminal-abc"
        assert popen.call_args.kwargs["env"]["OWNER_TYPE"] == "web"
        assert popen.call_args.kwargs["env"]["OWNER_REF"] == "u1"

    def test_missing_rotation_env_sets_nothing(self):
        tmux, _, _ = _spawned()

        assert not any(c.startswith("CLIENT_REF=") for c in tmux.new_session)
        assert not any(c.startswith("OWNER_TYPE=") for c in tmux.new_session)
        assert not any(c.startswith("OWNER_REF=") for c in tmux.new_session)

    def test_agent_view_disabled_so_a_tile_stays_on_its_own_conversation(self):
        """The agent view backgrounds the conversation into a daemon-hosted pty
        of its own geometry and lets a tile fork into someone else's transcript.
        One tile is one session on one conversation; the picker is the sidebar."""
        tmux, _, _ = _spawned()

        assert "CLAUDE_CODE_DISABLE_AGENT_VIEW=1" in tmux.new_session

    def test_an_existing_terminal_is_attached_not_restarted(self):
        # The bug this forecloses: a second `claude` on one conversation,
        # which is how two tiles crossed each other's threads.
        tmux, popen, _ = _spawned(alive=True)

        assert not any("new-session" in call for call in tmux.calls)
        assert popen.call_args.args[0][:5] == [
            "tmux", "-L", implementation.TMUX_SOCKET, "attach-session", "-d"
        ]

    def test_attach_detaches_whoever_held_the_session(self):
        _, popen, _ = _spawned(alive=True)

        assert "-d" in popen.call_args.args[0]

    def test_client_takes_the_pty_as_its_controlling_terminal(self):
        """Without this the winsize changes and nothing is ever signalled:
        SIGWINCH goes to a terminal's foreground process group, and a
        `setsid` child with no controlling tty has none. Every resize bug in
        the browser terminal traces back to this line."""
        _, popen, _ = _spawned()

        assert popen.call_args.kwargs["preexec_fn"] is implementation._take_controlling_tty

    def test_client_runs_as_a_browser_terminal(self):
        _, popen, _ = _spawned()

        assert popen.call_args.kwargs["env"]["TERM"] == "xterm-256color"

    def test_tmux_refusing_to_start_is_raised_not_swallowed(self):
        # A silent failure here is a tile that opens onto nothing.
        tmux = _TmuxRecorder(returncode=1, stderr="no server running")
        with pytest.raises(RuntimeError, match="no server running"):
            _spawned(tmux=tmux)

    def test_returns_process_and_master_fd(self):
        _, popen, result = _spawned()

        proc, master_fd = result
        assert proc is popen.return_value
        assert master_fd == 11

    def test_pty_wired_as_stdio_and_slave_closed_after_spawn(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.claude_cli._tmux", tmux), \
             patch("mind_templates.claude_cli.pty_session_alive", return_value=True), \
             patch("mind_templates.claude_cli.pty.openpty", return_value=(11, 22)), \
             patch("mind_templates.claude_cli.fcntl.ioctl"), \
             patch("mind_templates.claude_cli.os.close") as mock_close, \
             patch("mind_templates.claude_cli.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=999)
            implementation.spawn_pty(session_id="s1", model="sonnet", resume_sid="conv-1")

        kwargs = popen.call_args.kwargs
        assert kwargs["stdin"] == 22 and kwargs["stdout"] == 22 and kwargs["stderr"] == 22
        mock_close.assert_called_once_with(22)  # the slave belongs to the child now

    def test_initial_winsize_set_on_slave_before_spawn(self):
        # The client must start at the tile's size, or its first paint is
        # drawn for a geometry the browser doesn't have.
        tmux = _TmuxRecorder()
        with patch("mind_templates.claude_cli._tmux", tmux), \
             patch("mind_templates.claude_cli.pty_session_alive", return_value=True), \
             patch("mind_templates.claude_cli.pty.openpty", return_value=(11, 22)), \
             patch("mind_templates.claude_cli.fcntl.ioctl") as mock_ioctl, \
             patch("mind_templates.claude_cli.os.close"), \
             patch("mind_templates.claude_cli.subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=999)
            implementation.spawn_pty(session_id="s1", model="sonnet",
                                     resume_sid="conv-1", cols=132, rows=43)

        assert mock_ioctl.call_args.args[0] == 22  # the slave, before exec


class TestSpawnSurfaceEnv:
    """The stream-json process is stamped with the surface it serves.

    The gateway derives the label from the session's owner_type and ships
    it in the spawn payload; the env var is what the surface_inject hook
    reads per turn. A spawn from a gateway that predates the field falls
    back to the owner_type prefix, and a spawn with neither sets nothing —
    the hook's own default ("local") is for a human at a shell, not for a
    gateway that didn't say.
    """

    def _spawn_env(self, **kwargs):
        call = {"session_id": "s1", "model": "sonnet", "resume_sid": "conv-1"}
        call.update(kwargs)
        exec_mock = AsyncMock(return_value=MagicMock(pid=42))
        # spawn copies os.environ, and this suite may itself run inside a
        # surface-stamped process — mask the inherited value so the tests
        # see only what spawn sets.
        with patch("mind_templates.claude_cli.asyncio.create_subprocess_exec", exec_mock), \
             patch.dict("os.environ", {}, clear=False) as _env:
            os.environ.pop("HIVE_SURFACE", None)
            asyncio.run(implementation.spawn(**call))
        return exec_mock.call_args.kwargs["env"]

    def test_surface_from_the_gateway_reaches_the_env(self):
        env = self._spawn_env(surface="telegram", owner_type="telegram:uuid")
        assert env["HIVE_SURFACE"] == "telegram"

    def test_missing_surface_falls_back_to_the_owner_type_prefix(self):
        env = self._spawn_env(owner_type="discord:uuid")
        assert env["HIVE_SURFACE"] == "discord"

    def test_no_surface_and_no_owner_type_sets_nothing(self):
        env = self._spawn_env()
        assert "HIVE_SURFACE" not in env


class TestTmuxSessionLifecycle:
    def test_session_name_is_derived_from_the_session_id(self):
        assert implementation.tmux_session_name("abc-123") == "MIND_NAME-abc-123"

    def test_alive_follows_has_session(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.claude_cli._tmux", tmux):
            assert implementation.pty_session_alive("s1") is True
        assert tmux.calls[0][:2] == ["has-session", "-t"]

        with patch("mind_templates.claude_cli._tmux", _TmuxRecorder(returncode=1)):
            assert implementation.pty_session_alive("s1") is False

    def test_kill_ends_the_named_session(self):
        tmux = _TmuxRecorder()
        with patch("mind_templates.claude_cli._tmux", tmux):
            assert implementation.kill_pty_session("s1") is True
        assert tmux.calls[0] == ["kill-session", "-t", "=MIND_NAME-s1"]

    def test_kill_reports_when_there_was_nothing_to_kill(self):
        with patch("mind_templates.claude_cli._tmux", _TmuxRecorder(returncode=1)):
            assert implementation.kill_pty_session("s1") is False
