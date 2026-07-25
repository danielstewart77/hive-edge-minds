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
                      "resume_sid": resume_sid, "cols": cols, "rows": rows})
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
    task = asyncio.ensure_future(mind_server._reap_idle_ptys())
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
async def test_reaper_leaves_a_detached_terminal_inside_its_grace(spawned, clean_registry, monkeypatch):
    # Detached is the normal state between tiles; only the idle timeout ends it.
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    mind_server._detach_client(handle)
    handle.queue = None
    handle.detached_at = mind_server.time.time()

    await _run_one_reap_pass(monkeypatch)

    assert spawned.killed == []
    assert "sess-a" in clean_registry


@pytest.mark.asyncio
async def test_reaper_ends_a_terminal_nobody_came_back_to(spawned, clean_registry, monkeypatch):
    handle = mind_server._open_pty_session("sess-a", "sonnet", "conv-1", 80, 24)
    mind_server._detach_client(handle)
    handle.queue = None
    handle.detached_at = mind_server.time.time() - (mind_server._PTY_IDLE_TIMEOUT_S + 1)

    await _run_one_reap_pass(monkeypatch)

    assert spawned.killed == ["sess-a"]


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
