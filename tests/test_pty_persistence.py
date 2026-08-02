"""The web terminal's `claude` belongs to the session, not to the socket.

Every bug this covers had the same root cause: the terminal process was
owned by the websocket, so closing a browser tile killed the TUI mid-turn.
Reattaching then spawned a *second* process on the same conversation, which
is how two open tiles ended up crossing each other's threads and how "ask a
question, switch tiles, come back" produced a tile showing the question and
nothing after it.

The conversation now lives in a tmux session named for the hive session, and
an attach is a tmux client in a pty. So the split these tests hold to the
fire is: ending a *client* is detaching, and only `_teardown_pty` ends the
conversation.
"""

import os
import pty
import subprocess
import tempfile

import pytest

os.environ.setdefault("MIND_ID", "test-mind-id")
os.environ.setdefault("MIND_NAME", "example")
os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="pty-persist-test-"))

import mind_server  # noqa: E402


class _FakeProc:
    """A process that stays alive until someone deliberately ends it."""

    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture
def clean_registry():
    mind_server._ptys.clear()
    yield mind_server._ptys
    for sid in list(mind_server._ptys):
        handle = mind_server._ptys.pop(sid)
        if handle.master_fd is not None:
            try:
                os.close(handle.master_fd)
            except OSError:
                pass


@pytest.fixture
def spawned(monkeypatch, clean_registry):
    """Patch the mind's tmux plumbing: real pty fds, fake processes."""

    class _Calls(list):
        """The spawn log, with the tmux side-effects the tests assert on."""

    calls = _Calls()
    opened = []
    killed = []
    live = {"session": True}

    def _fake_spawn_pty(session_id, model, resume_sid=None, cols=80, rows=24, **kwargs):
        calls.append({"session_id": session_id, "model": model,
                      "resume_sid": resume_sid, "cols": cols, "rows": rows,
                      "system_prompt": kwargs.get("system_prompt", "")})
        master_fd, slave_fd = pty.openpty()
        opened.append((master_fd, slave_fd))
        return _FakeProc(pid=4000 + len(calls)), master_fd

    monkeypatch.setattr(mind_server.impl, "spawn_pty", _fake_spawn_pty, raising=False)
    monkeypatch.setattr(mind_server.impl, "kill_pty_session",
                        lambda session_id: killed.append(session_id) or True, raising=False)
    monkeypatch.setattr(mind_server.impl, "pty_session_alive",
                        lambda session_id: live["session"], raising=False)
    monkeypatch.setattr(mind_server, "_load_registry_and_mcp_config", lambda: (None, None, ""))
    monkeypatch.setattr(mind_server, "_register_pty_reader", lambda handle: None)
    calls.killed = killed          # type: ignore[attr-defined]
    calls.live = live              # type: ignore[attr-defined]
    yield calls
    for master_fd, slave_fd in opened:
        for fd in (master_fd, slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_second_attach_keeps_the_same_conversation(spawned, clean_registry):
    # Reattaching must land on the running `claude`. Starting a second one
    # on the same conversation is what let two tiles cross their threads.
    first = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    second = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 120, 40)

    assert first is second
    assert spawned.killed == []  # the terminal was never ended, only re-viewed


def test_second_attach_replaces_the_viewing_client(spawned, clean_registry):
    # One conversation, one keyboard: the tile that had it loses its client
    # so the two can't type into the same pane.
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    first_client = handle.proc

    mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 120, 40)

    assert first_client.terminated is True
    assert handle.proc is not first_client
    assert (handle.cols, handle.rows) == (120, 40)


def test_displaced_socket_is_told_it_was_replaced(spawned, clean_registry):
    # Before its client dies, so the old tile reads eviction rather than
    # "the terminal exited" and reconnects instead of going dark.
    import asyncio

    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    old_queue: asyncio.Queue = asyncio.Queue()
    handle.queue = old_queue

    mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)

    assert old_queue.get_nowait() is mind_server._PTY_EVICTED


def test_separate_sessions_get_separate_terminals(spawned, clean_registry):
    a = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    b = mind_server._open_pty_session("sess-b", "sonnet", "conv-2", 80, 24)

    assert a is not b
    assert a.master_fd != b.master_fd
    assert a.tmux_name != b.tmux_name
    assert set(clean_registry) == {"sess-a", "sess-b"}


def test_detaching_leaves_the_conversation_running(spawned, clean_registry):
    # The whole point: the turn in flight survives a closed tab.
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    client = handle.proc

    mind_server._detach_client(handle)

    assert client.terminated is True
    assert handle.master_fd is None
    assert spawned.killed == []
    assert "sess-a" in clean_registry


def test_push_reaches_the_attached_queue(spawned, clean_registry):
    import asyncio

    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    queue: asyncio.Queue = asyncio.Queue()
    handle.queue = queue

    handle.push(b"live bytes")

    assert queue.get_nowait() == b"live bytes"


def test_teardown_kills_the_tmux_session_and_forgets_it(spawned, clean_registry):
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    client = handle.proc

    assert mind_server._teardown_pty("sess-a") is True

    assert client.terminated is True
    assert spawned.killed == ["sess-a"]
    assert "sess-a" not in clean_registry
    assert mind_server._teardown_pty("sess-a") is False


def test_teardown_escalates_when_terminate_is_ignored(spawned, clean_registry):
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)

    def _stubborn_wait(timeout=None):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=timeout or 5)

    killed = []
    handle.proc.terminate = lambda: None
    handle.proc.kill = lambda: killed.append(True)
    handle.proc.wait = lambda timeout=None: (
        _stubborn_wait(timeout) if not killed else 0
    )

    mind_server._teardown_pty("sess-a")

    assert killed == [True]


def test_resume_sid_is_passed_through_on_first_spawn(spawned, clean_registry):
    mind_server._open_pty_session("sess-a", "opus", "claude-existing", 100, 30)

    assert spawned[0]["resume_sid"] == "claude-existing"
    assert spawned[0]["model"] == "opus"
    assert (spawned[0]["cols"], spawned[0]["rows"]) == (100, 30)


async def _run_one_reap_pass(monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(mind_server, "_PTY_REAP_INTERVAL_S", 0.01)
    task = asyncio.ensure_future(mind_server._reap_dead_ptys())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reaper_ends_a_terminal_whose_claude_exited(spawned, clean_registry, monkeypatch):
    # The client is gone the moment a tile detaches, so liveness is tmux's
    # answer to give — asking the client would reap every idle session.
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    mind_server._detach_client(handle)
    handle.queue = None
    spawned.live["session"] = False

    await _run_one_reap_pass(monkeypatch)

    assert spawned.killed == ["sess-a"]
    assert "sess-a" not in clean_registry


@pytest.mark.asyncio
async def test_a_detached_terminal_outlives_any_amount_of_idleness(
    spawned, clean_registry, monkeypatch
):
    """Detached is the normal state between tiles, and it has no deadline.

    A terminal used to be collected after an hour unattached, which meant a
    conversation could be destroyed by the user's absence alone — including
    a freshly rotated one whose carry-forward had not yet reached a
    transcript. Nothing but an explicit close ends a live terminal now, so
    the length of the gap is not a property the reaper is allowed to read.
    """
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    mind_server._detach_client(handle)
    handle.queue = None
    handle.detached_at = mind_server.time.time() - (365 * 24 * 60 * 60)

    await _run_one_reap_pass(monkeypatch)

    assert spawned.killed == []
    assert "sess-a" in clean_registry
    assert clean_registry["sess-a"].alive


@pytest.mark.asyncio
async def test_kill_session_tears_down_a_terminal_only_session(spawned, clean_registry):
    # A session born in the browser terminal has no stream-json process, so
    # the old code 404'd and left `claude` running forever.
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    mind_server._sessions.pop("sess-a", None)

    result = await mind_server.kill_session("sess-a")

    assert result == {"session_id": "sess-a", "status": "closed"}
    assert handle.proc is None
    assert spawned.killed == ["sess-a"]
    assert "sess-a" not in clean_registry


@pytest.mark.asyncio
async def test_kill_session_still_404s_when_nothing_exists(clean_registry, monkeypatch):
    monkeypatch.setattr(mind_server.impl, "kill_pty_session", lambda sid: False, raising=False)
    result = await mind_server.kill_session("no-such-session")

    assert getattr(result, "status_code", None) == 404


class TestCarryForwardOnAttach:
    """A rotation seeds a pane with context that exists nowhere else.

    The seed is a system prompt on one process and a file deleted the instant
    that process reads it, so it reaches no transcript. Whatever kills the
    pane before the user's first turn — a restart, a crash, a tab closed and
    never reopened — used to take the whole carry-forward with it, and the
    reattach resumed a conversation id with nothing behind it. comms holds
    the seed on the session row until a turn proves the rotation took; the
    mind asks for it as it starts the terminal.
    """

    @pytest.mark.asyncio
    async def test_a_stored_carry_forward_is_applied_to_the_new_terminal(
        self, spawned, clean_registry, monkeypatch
    ):
        async def fetch(session_id, claude_sid):
            assert (session_id, claude_sid) == ("sess-a", "conv-rotated")
            return "<soul>carried forward</soul>"

        monkeypatch.setattr(mind_server, "_fetch_carry_forward", fetch)
        # A cold open: tmux holds no session yet, which is the only case a
        # stored seed can still be applied to.
        spawned.live["session"] = False

        await mind_server._open_pty_session_seeded(
            "sess-a", "opus", "conv-rotated", 80, 24
        )

        assert spawned[0]["system_prompt"] == "<soul>carried forward</soul>"

    @pytest.mark.asyncio
    async def test_a_terminal_with_nothing_owed_is_started_unseeded(
        self, spawned, clean_registry, monkeypatch
    ):
        """The ordinary case: no rotation outstanding, or its first turn
        already landed and the transcript carries the context by itself."""
        async def fetch(session_id, claude_sid):
            return ""

        monkeypatch.setattr(mind_server, "_fetch_carry_forward", fetch)
        # A cold open: tmux holds no session yet, which is the only case a
        # stored seed can still be applied to.
        spawned.live["session"] = False

        await mind_server._open_pty_session_seeded("sess-a", "opus", "conv-1", 80, 24)

        assert spawned[0]["system_prompt"] == ""

    @pytest.mark.asyncio
    async def test_a_live_terminal_is_never_asked_for_a_carry_forward(
        self, spawned, clean_registry, monkeypatch
    ):
        """Reattaching to a running pane is the common case, and its harness
        already holds its context — the answer could only be discarded. Asking
        anyway puts a gateway round trip between the socket's accept and the
        terminal's first painted byte, every single time.
        """
        asked: list = []

        async def fetch(session_id, claude_sid):
            asked.append(session_id)
            return "<soul>should never be applied</soul>"

        monkeypatch.setattr(mind_server, "_fetch_carry_forward", fetch)
        spawned.live["session"] = True  # tmux already holds this conversation

        await mind_server._open_pty_session_seeded(
            "sess-a", "opus", "conv-1", 80, 24
        )

        assert asked == []

    @pytest.mark.asyncio
    async def test_a_comms_that_cannot_be_reached_still_opens_the_terminal(
        self, spawned, clean_registry, monkeypatch
    ):
        """Losing the seed is bad; refusing to open the terminal is worse.

        The transcript is the primary recovery path and needs no help from
        comms, so an unreachable gateway degrades to an unseeded pane.
        """
        async def fetch(session_id, claude_sid):
            raise OSError("comms is down")

        monkeypatch.setattr(mind_server, "_fetch_carry_forward", fetch)
        # A cold open: tmux holds no session yet, which is the only case a
        # stored seed can still be applied to.
        spawned.live["session"] = False

        await mind_server._open_pty_session_seeded("sess-a", "opus", "conv-1", 80, 24)

        assert spawned[0]["system_prompt"] == ""
        assert "sess-a" in clean_registry


class TestTheCarryForwardLookupItself:
    """The client half of the contract, actually executed.

    Every test above monkeypatches `_fetch_carry_forward` out, which is right
    for them — they are about what the mind does with an answer. But it left
    the request itself never once made: the URL, the conversation id, the
    admin header and the unwrap were all only assertions in a docstring. A
    mind that asked the wrong gateway, dropped the bearer, or read the wrong
    key would have shipped green.
    """

    @staticmethod
    async def _gateway(handler):
        """A real HTTP server on a real port, speaking the comms route."""
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/sessions/{session_id}/carry-forward", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        return runner, f"http://127.0.0.1:{port}"

    @pytest.mark.asyncio
    async def test_the_lookup_asks_the_right_question_and_unwraps_the_answer(
        self, monkeypatch
    ):
        from aiohttp import web

        seen = {}

        async def handler(request):
            seen["path"] = request.path
            seen["claude_sid"] = request.query.get("claude_sid")
            seen["auth"] = request.headers.get("Authorization")
            return web.json_response(
                {"session_id": "sess-a", "carry_forward": "<soul>carried</soul>"}
            )

        runner, base = await self._gateway(handler)
        try:
            monkeypatch.setenv("COMMS_URL", base + "/")
            monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")

            got = await mind_server._fetch_carry_forward("sess-a", "conv-rotated")
        finally:
            await runner.cleanup()

        assert got == "<soul>carried</soul>"
        assert seen["path"] == "/sessions/sess-a/carry-forward"
        # The conversation id is the whole point — a lookup that drops it
        # would be answered with some other conversation's context.
        assert seen["claude_sid"] == "conv-rotated"
        # The route is admin-gated; without this the mind gets a 401 and
        # silently opens every rotated terminal unseeded.
        assert seen["auth"] == "Bearer admin-token"

    @pytest.mark.asyncio
    async def test_a_refusal_is_read_as_nothing_owed_rather_than_raised(
        self, monkeypatch
    ):
        from aiohttp import web

        async def handler(request):
            return web.json_response({"detail": "nope"}, status=401)

        runner, base = await self._gateway(handler)
        try:
            monkeypatch.setenv("COMMS_URL", base)
            monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "wrong-token")

            assert await mind_server._fetch_carry_forward("sess-a", "conv-1") == ""
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_nothing_is_asked_without_a_gateway_a_token_or_a_conversation(
        self, monkeypatch
    ):
        """Three ways to have no question worth asking. Each must short out
        before the request, not after — a mind with no admin token would
        otherwise spend five seconds per terminal open earning a 401."""
        async def handler(request):  # pragma: no cover - must never run
            raise AssertionError("the lookup should not have been attempted")

        runner, base = await self._gateway(handler)
        try:
            monkeypatch.setenv("COMMS_URL", base)
            monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
            assert await mind_server._fetch_carry_forward("sess-a", "") == ""

            monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "")
            assert await mind_server._fetch_carry_forward("sess-a", "conv-1") == ""

            monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
            monkeypatch.setenv("COMMS_URL", "")
            assert await mind_server._fetch_carry_forward("sess-a", "conv-1") == ""
        finally:
            await runner.cleanup()
