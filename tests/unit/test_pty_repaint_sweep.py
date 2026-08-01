"""Tests for the periodic tmux repaint sweep on mind_server.py.

A tmux client only repaints the cells tmux believes changed. When the pane
shifts text up a line, tmux emits a scroll-region shortcut and paints the
new bottom row alone — every other cell is left to the emulator, and where
the emulator gets that wrong the stale characters survive every subsequent
partial paint. tmux's own model stays clean, so asking it to redraw the
whole client clears them.

These cover the sweep that does the asking: which sessions it touches, that
it does not wait for output to go quiet, that a failing refresh neither
stops the sweep nor costs the other sessions their turn, and that the
refresh reaches the module the service actually imports.
"""
import asyncio
import importlib
import importlib.util
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def _mock_mind_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MIND_ID", "ada")
    monkeypatch.setenv("MIND_NAME", "ada")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("PTY_REPAINT_SWEEP", "1")
    monkeypatch.delenv("PTY_REPAINT_INTERVAL_SECONDS", raising=False)


@pytest.fixture()
def mind_server(_mock_mind_env):
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import mind_server as module
            importlib.reload(module)
            module.impl.tmux_session_name = lambda session_id: f"ada-{session_id}"
            yield module


def _handle(mind_server, session_id: str, *, attached: bool):
    """A pty handle registered as tmux-backed, with or without a live tile.

    `queue` is the attachment: the output pump sets it on connect and clears
    it on disconnect, so it is already the answer to "is a browser watching
    this right now" without any new bookkeeping.
    """
    handle = mind_server._PtyHandle(
        session_id, f"ada-{session_id}", "sid-" + session_id, 112, 30, model="sonnet",
    )
    handle.queue = asyncio.Queue() if attached else None
    mind_server._ptys[session_id] = handle
    return handle


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


class TestRepaintSweep:
    def test_attached_session_is_refreshed_at_the_configured_rate(self, mind_server):
        """Requirement 1: an attached tile is refreshed about twice a second.

        Bounded on both sides. Too slow fails the requirement; too fast is a
        busy loop shelling out to tmux thousands of times a second, which no
        one would notice from a count-at-least assertion.
        """
        assert mind_server._PTY_REPAINT_INTERVAL_S == 0.5, "default is not twice a second"

        _handle(mind_server, "sess-1", attached=True)
        mind_server.impl.refresh_pty_client = MagicMock(return_value=True)
        interval, window = 0.01, 0.10

        async def run():
            mind_server._PTY_REPAINT_INTERVAL_S = interval
            task = asyncio.ensure_future(mind_server._sweep_pty_repaints())
            started = time.monotonic()
            await asyncio.sleep(window)
            elapsed = time.monotonic() - started
            task.cancel()
            return elapsed

        elapsed = _run(run)

        calls = mind_server.impl.refresh_pty_client.call_args_list
        ceiling = int(elapsed / interval) + 3
        assert 3 <= len(calls) <= ceiling, (
            f"{len(calls)} sweeps in {elapsed:.3f}s at a {interval}s interval"
        )
        assert all(c.args[0] == "sess-1" for c in calls)

    def test_refreshes_while_output_streams_without_pause(self, mind_server):
        """Requirement 2: refreshing is not gated on output going quiet."""
        handle = _handle(mind_server, "sess-1", attached=True)
        mind_server.impl.refresh_pty_client = MagicMock(return_value=True)

        async def run():
            mind_server._PTY_REPAINT_INTERVAL_S = 0.01
            task = asyncio.ensure_future(mind_server._sweep_pty_repaints())
            # A turn in flight: the spinner never lets the pane fall silent,
            # which is exactly when the garbage accumulates.
            for _ in range(40):
                handle.push(b"\x1b[2K.")
                await asyncio.sleep(0.002)
            task.cancel()

        _run(run)

        assert mind_server.impl.refresh_pty_client.call_count >= 3

    def test_unattached_session_is_never_refreshed(self, mind_server):
        """Requirement 3: a session with no tile attached is left alone."""
        _handle(mind_server, "sess-detached", attached=False)
        _handle(mind_server, "sess-live", attached=True)
        mind_server.impl.refresh_pty_client = MagicMock(return_value=True)

        mind_server._repaint_attached_ptys()

        refreshed = [c.args[0] for c in mind_server.impl.refresh_pty_client.call_args_list]
        assert refreshed == ["sess-live"]

    def test_detaching_stops_the_refreshing(self, mind_server):
        """Requirement 4: once the last tile detaches, refreshing stops."""
        handle = _handle(mind_server, "sess-1", attached=True)
        mind_server.impl.refresh_pty_client = MagicMock(return_value=True)

        mind_server._repaint_attached_ptys()
        assert mind_server.impl.refresh_pty_client.call_count == 1

        handle.queue = None  # what the pump does on disconnect
        mind_server._repaint_attached_ptys()

        assert mind_server.impl.refresh_pty_client.call_count == 1

    def test_failing_refresh_costs_only_that_session_that_pass(self, mind_server):
        """Requirement 5: a failed refresh ends neither the sweep nor a session.

        Two attached sessions with the first one raising: the second still
        gets its repaint in the same pass, and the sweep is still running
        afterwards. One session's dead terminal must not cost every other
        session every repaint behind it.
        """
        _handle(mind_server, "sess-broken", attached=True)
        _handle(mind_server, "sess-ok", attached=True)

        def refresh(session_id):
            if session_id == "sess-broken":
                raise OSError("no server running")
            return True

        mind_server.impl.refresh_pty_client = MagicMock(side_effect=refresh)

        painted = mind_server._repaint_attached_ptys()
        assert painted == 1
        tried = [c.args[0] for c in mind_server.impl.refresh_pty_client.call_args_list]
        assert tried == ["sess-broken", "sess-ok"], "a failure skipped the rest of the pass"

        async def run():
            mind_server._PTY_REPAINT_INTERVAL_S = 0.01
            task = asyncio.ensure_future(mind_server._sweep_pty_repaints())
            await asyncio.sleep(0.06)
            still_running = not task.done()
            task.cancel()
            return still_running

        assert _run(run), "one failed refresh ended the sweep"
        assert "sess-broken" in mind_server._ptys
        assert "sess-ok" in mind_server._ptys

    def test_switch_governs_whether_the_sweep_runs(self, mind_server, monkeypatch):
        """Requirement 6: on for a mind that asked, off for every other one."""
        def start():
            async def run():
                task = mind_server._start_pty_repaint_sweep()
                if task is not None:
                    task.cancel()
                return task
            return _run(run)

        assert mind_server._PTY_REPAINT_ENABLED is True
        assert start() is not None, "switched on, but no sweep was started"

        monkeypatch.delenv("PTY_REPAINT_SWEEP", raising=False)
        importlib.reload(mind_server)
        mind_server.impl.tmux_session_name = lambda session_id: f"ada-{session_id}"

        assert mind_server._PTY_REPAINT_ENABLED is False
        assert start() is None, "switched off, but a sweep was started anyway"

    def test_switch_off_survives_a_malformed_interval(self, mind_server, monkeypatch):
        """Requirement 6: a mind that never asked for the sweep still boots.

        The interval is parsed at import, before the switch is consulted, so
        a typo in it would otherwise crash every mind sharing this file.
        """
        monkeypatch.delenv("PTY_REPAINT_SWEEP", raising=False)
        monkeypatch.setenv("PTY_REPAINT_INTERVAL_SECONDS", "half a second")
        importlib.reload(mind_server)  # must not raise

        assert mind_server._PTY_REPAINT_INTERVAL_S == 0.5


def _load_template(path: Path):
    spec = importlib.util.spec_from_file_location(f"tmpl_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _harness_templates():
    return sorted(REPO_ROOT.glob("mind_templates/*_cli.py"))


class TestRefreshPtyClientContract:
    """Requirement 1, mechanism half: what the refresh actually asks tmux for.

    Every test above assigns the refresh onto a MagicMock module, so the mock
    is both the stub and the assertion target and the real function is never
    imported. This drives the real one with tmux itself faked out.
    """

    @pytest.mark.parametrize("path", _harness_templates(), ids=lambda p: p.stem)
    def test_lists_clients_then_refreshes_each_by_tty(self, path, monkeypatch):
        module = _load_template(path)
        calls = []

        def fake_tmux(*args, env=None):
            calls.append(args)
            if args[0] == "list-clients":
                return MagicMock(returncode=0, stdout="/dev/pts/3\n/dev/pts/7\n")
            return MagicMock(returncode=0, stdout="")

        monkeypatch.setattr(module, "_tmux", fake_tmux)

        assert module.refresh_pty_client("sess-1") is True

        name = module.tmux_session_name("sess-1")
        assert calls[0] == ("list-clients", "-t", f"={name}", "-F", "#{client_tty}"), (
            "targeting must be exact-match, or one session answers for another"
        )
        assert calls[1:] == [
            ("refresh-client", "-t", "/dev/pts/3"),
            ("refresh-client", "-t", "/dev/pts/7"),
        ], "every attached client has to be repainted, not just the first"

    @pytest.mark.parametrize("path", _harness_templates(), ids=lambda p: p.stem)
    def test_reports_nothing_painted_when_no_client_is_attached(self, path, monkeypatch):
        """A session nobody is watching is not a failure — there is just
        nothing to repaint, and no refresh-client should be issued."""
        module = _load_template(path)
        calls = []

        def fake_tmux(*args, env=None):
            calls.append(args)
            return MagicMock(returncode=0, stdout="\n")

        monkeypatch.setattr(module, "_tmux", fake_tmux)

        assert module.refresh_pty_client("sess-1") is False
        assert [c[0] for c in calls] == ["list-clients"]

    @pytest.mark.parametrize("path", _harness_templates(), ids=lambda p: p.stem)
    def test_reports_failure_when_the_session_is_gone(self, path, monkeypatch):
        module = _load_template(path)

        def fake_tmux(*args, env=None):
            return MagicMock(returncode=1, stdout="", stderr="can't find session")

        monkeypatch.setattr(module, "_tmux", fake_tmux)

        assert module.refresh_pty_client("sess-1") is False


class TestRepaintIsActuallyInstalled:
    """Requirement 1, production half.

    `mind_server` imports `minds.<name>.implementation` — a copy `setup.sh`
    made at install time, which it then refuses to overwrite. Adding the
    refresh to `mind_templates/` alone leaves the sweep calling a method the
    running module does not have: it logs that it is on and repaints
    nothing. The unit tests above cannot see this, because they mock that
    module and a MagicMock grows whatever attribute it is asked for.
    """

    def test_every_harness_template_offers_the_refresh(self):
        templates = _harness_templates()
        assert templates, "no harness templates found"
        missing = [p.name for p in templates
                   if "def refresh_pty_client" not in p.read_text()]
        assert not missing, f"templates without refresh_pty_client: {missing}"

    def test_installed_implementations_offer_the_refresh(self):
        installed = [p for p in sorted(REPO_ROOT.glob("minds/*/implementation.py"))
                     if p.parent.name != "example"]
        if not installed:
            pytest.skip("no mind installed here — minds/<name>/ is per-host")
        missing = [p.parent.name for p in installed
                   if "def refresh_pty_client" not in p.read_text()]
        assert not missing, (
            f"installed minds missing refresh_pty_client: {missing} — "
            "the template was updated but the install was not"
        )
