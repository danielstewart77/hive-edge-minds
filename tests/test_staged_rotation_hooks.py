"""The pane's half of a staged rotation: when it is armed and when it fires.

A terminal rotation now spans three hook processes with an unbounded gap in
the middle — the Stop that stages it, the Stop/SubagentStop that report what
is still running, and the UserPromptSubmit that fires it on the user's next
typed message. Nothing is shared between them but files, and each of them
can be killed by the respawn the third one triggers.

The hooks live in ``~/.claude/hooks`` and are not tracked (edit is deploy),
so they are loaded by path. On a host without them installed there is
nothing to test and the module skips — which is also why the assertions here
are about behaviour the hooks own outright, not about the harness.

Each case is pinned at the site that actually runs it. A helper called only
by tests, a threshold never evaluated, a decision channel replaced by a list
— each of those is a test that passes whether or not the pane behaves, and
this file has been through a mutation pass to keep them out.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path.home() / ".claude" / "hooks"
FIRE_HOOK = HOOKS_DIR / "rotation_fire.py"
STATE_MODULE = HOOKS_DIR / "rotation_state.py"

pytestmark = pytest.mark.skipif(
    not (FIRE_HOOK.is_file() and STATE_MODULE.is_file()),
    reason="staged-rotation hooks not installed on this host",
)


@pytest.fixture()
def hooks(tmp_path, monkeypatch):
    """The hook modules, with their state directory pointed at a temp tree."""
    monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(HOOKS_DIR))
    for name in ("rotation_state", "rotation_fire", "bg_snapshot"):
        sys.modules.pop(name, None)
    import rotation_state
    import rotation_fire
    assert rotation_state.state_dir() == tmp_path / "data" / "auto-remember"
    yield rotation_state, rotation_fire
    for name in ("rotation_state", "rotation_fire", "bg_snapshot"):
        sys.modules.pop(name, None)


def _stage(state, sid="conv-1", ready=True, forced=True):
    state.stage_marker(sid, client_type="web", client_ref="term-1", forced=forced)
    if ready:
        state.mark_ready(sid)
    return sid


def _event(sid="conv-1", prompt="carry on"):
    return {"session_id": sid, "prompt": prompt, "transcript_path": ""}


def _fire(rotation_fire, monkeypatch, event, capsys=None):
    """Run the fire hook's decision, capturing whether it asked to rotate."""
    fired: list[tuple] = []
    monkeypatch.setattr(
        rotation_fire, "_detach_and_fire",
        lambda marker, sid, prompt: fired.append((marker, sid, prompt)),
    )
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(event)))
    rotation_fire.main()
    return fired


class _Stdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


def _transcript(path: Path, tokens: int) -> Path:
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {"usage": {"input_tokens": tokens, "output_tokens": 1}},
    }) + "\n")
    return path


# ---------------------------------------------------------------------------


def test_a_terminal_under_the_threshold_stages_nothing(tmp_path, monkeypatch):
    """Requirement 1: it is *crossing the threshold* that prepares a rotation.
    A conversation with room left prepares none — and the threshold is the
    only thing separating the two, so it has to be the thing evaluated."""
    monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    monkeypatch.setenv("OWNER_TYPE", "web")
    monkeypatch.setenv("CLIENT_REF", "term-1")
    monkeypatch.syspath_prepend(str(HOOKS_DIR))
    for name in ("rotation_state", "rotation_check"):
        sys.modules.pop(name, None)
    import rotation_check
    import rotation_state

    # No force: the threshold decides, which is the point.
    rotation_check._run({
        "session_id": "conv-small",
        "transcript_path": str(_transcript(tmp_path / "small.jsonl", 1_000)),
    })

    assert rotation_state.read_marker("conv-small") is None
    for name in ("rotation_state", "rotation_check"):
        sys.modules.pop(name, None)


def test_the_rotation_is_pending_before_the_summary_is_written(tmp_path, monkeypatch):
    """Requirement 2: the marker exists from the moment the threshold check
    passes, not after the minutes of composition that follow it — otherwise a
    prompt reply lands in a conversation that is not yet pending anything."""
    monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    monkeypatch.setenv("OWNER_TYPE", "web")
    monkeypatch.setenv("CLIENT_REF", "term-1")
    monkeypatch.syspath_prepend(str(HOOKS_DIR))
    for name in ("rotation_state", "rotation_check"):
        sys.modules.pop(name, None)
    import rotation_check
    import rotation_state

    order: list[str] = []
    real_stage = rotation_state.stage_marker
    seen: dict[str, object] = {}

    def staged(sid, **kw):
        order.append("staged")
        return real_stage(sid, **kw)

    def compose(*_a, **_k):
        order.append("compose")
        # What the fire hook would find if the user replied right now, read
        # while composition is still running rather than after it returns.
        seen["marker"] = rotation_state.read_marker("conv-1")
        raise SystemExit("stop before the expensive work")

    monkeypatch.setattr(rotation_state, "stage_marker", staged)
    monkeypatch.setattr(rotation_check, "_wait_for_quiescence", compose)
    # The gateway pre-flight below the threshold check would otherwise decide
    # this test's outcome from whatever comms happens to be running on the
    # host — "term-1" is not a live pane anywhere, so a reachable gateway
    # answers no and nothing stages. What is under test here is the ordering
    # of staging against composition, so the pre-flight is pinned.
    monkeypatch.setattr(rotation_check, "_comms_has_active_session", lambda *a, **k: True)

    with pytest.raises(SystemExit):
        rotation_check._run({
            "session_id": "conv-1",
            "transcript_path": str(_transcript(tmp_path / "t.jsonl", 10_000_000)),
        })

    assert order == ["staged", "compose"], (
        "composition began before the rotation was pending"
    )
    assert seen["marker"] is not None
    for name in ("rotation_state", "rotation_check"):
        sys.modules.pop(name, None)


@pytest.mark.parametrize("envelope", [
    "<task-notification>\n<status>completed</status>\n</task-notification>",
    "<system-reminder>you have a memory</system-reminder>",
    "<local-command-caveat>Caveat: the messages below were generated…",
    "<local-command-stdout>ok</local-command-stdout>",
    "<local-command-stderr>bad</local-command-stderr>",
    "<command-name>/compact</command-name>",
    "<command-message>compacting…</command-message>",
    "<user-memory-input>a note</user-memory-input>",
])
def test_a_message_the_user_did_not_type_does_not_rotate(hooks, monkeypatch, envelope):
    """Requirement 3: a finished background agent reports in through the same
    prompt pipeline, and so does every slash command's own plumbing. Firing on
    one rotates a pane nobody is sitting at, and the successor's opening turn
    is machine XML."""
    state, fire = hooks
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    _stage(state)

    assert _fire(fire, monkeypatch, _event(prompt=envelope)) == []
    assert state.read_marker("conv-1") is not None, "the rotation stopped being pending"

    assert len(_fire(fire, monkeypatch, _event(prompt="ok, carry on"))) == 1


def test_the_seed_reaches_the_pane_as_a_user_turn_not_a_system_prompt(
    tmp_path, monkeypatch
):
    """Requirement 4: a staged rotation's seed carries the user's own message,
    so it enters the harness positionally. As a system prompt it submits
    nothing and reaches no transcript — the pane would open at an empty prompt
    with what they typed gone.

    Driven through ``rotate_pty_session``, which is what the route calls. The
    entry point is chosen there, and a test that reached only the helper it
    calls would pass with that choice reverted.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from mind_templates import claude_cli

    respawns: list[list[str]] = []

    def fake_tmux(*args, env=None):
        if args and args[0] == "respawn-pane":
            respawns.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(claude_cli, "pty_session_alive", lambda _sid: True)
    monkeypatch.setattr(claude_cli, "_tmux", fake_tmux)

    seed = "SUMMARY OF THE OLD CONVERSATION"
    typed = "what were we doing?"
    assert claude_cli.rotate_pty_session(
        session_id="sess-1", new_claude_sid="conv-2", model="opus",
        user_prompt=f"{seed}\n\n{typed}",
    ) is True

    command = respawns[0][-1]
    assert "--append-system-prompt" not in command
    assert 'exec claude' in command
    # Positional, which is what makes it a turn the harness submits.
    assert '--session-id conv-2 "$seed"' in command
    # The seed still travels in a file: tmux refuses a long respawn command.
    assert seed not in command
    written = (tmp_path / "rotation-seeds" / "conv-2.txt").read_text()
    assert written.endswith(typed)

    # A fresh terminal has no user message to carry, and standing context
    # belongs in a system prompt.
    respawns.clear()
    assert claude_cli.rotate_pty_session(
        session_id="sess-1", new_claude_sid="conv-3", model="opus",
        system_prompt=seed,
    ) is True
    assert "--append-system-prompt" in respawns[0][-1]


def test_the_route_hands_the_typed_message_through_to_the_pane(monkeypatch):
    """Requirement 4, at the seam: the gateway sends the seed and the message
    as ``user_prompt``, and the route is where that could be dropped without
    anything below it noticing — the pane would open on a system prompt and
    the message would exist nowhere."""
    import asyncio

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import mind_server

    seen: dict[str, object] = {}

    def fake_rotate(**kwargs):
        seen.update(kwargs)
        return True

    handle = type("H", (), {"model": "opus", "claude_sid": "conv-old"})()
    monkeypatch.setitem(mind_server._ptys, "sess-1", handle)
    monkeypatch.setattr(mind_server.impl, "rotate_pty_session", fake_rotate,
                        raising=False)
    monkeypatch.setattr(mind_server, "_load_registry_and_mcp_config",
                        lambda: (None, None, ""))

    class _Request:
        async def json(self):
            return {
                "new_claude_sid": "conv-2",
                "model": "opus",
                "user_prompt": "SUMMARY\n\nwhat were we doing?",
            }

    result = asyncio.run(mind_server.rotate_pty("sess-1", _Request()))

    assert result["rotated"] is True
    assert seen["user_prompt"] == "SUMMARY\n\nwhat were we doing?"


def test_a_rotation_waits_while_background_work_is_still_running(hooks, monkeypatch, capsys):
    """Requirement 5: the respawn kills the pane's process group and every
    background agent and shell in it, so a rotation may not fire over live
    work — and what is holding it is reported, on the channel the model reads.
    """
    state, fire = hooks
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    _stage(state)
    state.record_background_tasks("conv-1", [
        {"id": "a1", "type": "subagent", "status": "running",
         "description": "Refute rotation spec"},
        {"id": "b2", "type": "shell", "status": "completed", "command": "sleep 1"},
    ])

    assert _fire(fire, monkeypatch, _event()) == []
    assert state.read_marker("conv-1") is not None

    # Not a captured callback: `additionalContext` is what reaches the model
    # and `systemMessage` is UI-only, so the channel is the assertion.
    emitted = json.loads(capsys.readouterr().out)
    said = emitted["hookSpecificOutput"]["additionalContext"]
    assert emitted["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Refute rotation spec" in said
    assert "sleep 1" not in said, "a finished task was reported as holding it"

    # Drained: the next typed message rotates.
    state.record_background_tasks("conv-1", [])
    assert len(_fire(fire, monkeypatch, _event())) == 1


def test_a_message_that_beats_the_summary_is_never_held_back(hooks, monkeypatch, capsys):
    """Requirement 6: the message is never blocked. Composition runs to six
    minutes and this hook gates submission, so a hook that waits it out is
    indistinguishable from a hung terminal. It waits briefly, then lets the
    message through with the rotation still staged for the next turn."""
    state, fire = hooks
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    _stage(state, ready=False)

    # The bound itself is the behaviour: composition runs to six minutes, and
    # anything approaching that is a frozen pane rather than a wait.
    assert state.COMPOSE_WAIT_SECONDS <= 15

    monkeypatch.setattr(state, "COMPOSE_WAIT_SECONDS", 0.5)
    started = time.time()
    assert _fire(fire, monkeypatch, _event(prompt="what were we doing?")) == []
    assert time.time() - started < 5, "the prompt was held for the composition"

    emitted = json.loads(capsys.readouterr().out)
    # Whatever it says, it must not be a refusal: a decision of "block" stops
    # the message reaching the harness at all.
    assert emitted.get("decision") != "block"
    assert "additionalContext" in emitted["hookSpecificOutput"]

    # Still owed, so the next turn fires it rather than paying for a second
    # composition.
    assert state.read_marker("conv-1") is not None
    state.mark_ready("conv-1")
    assert len(_fire(fire, monkeypatch, _event(prompt="still there?"))) == 1


def test_a_rotation_the_gateway_refused_leaves_the_conversation_alone(hooks, monkeypatch):
    """Requirement 7: nothing was replaced, so the message is answered where it
    was typed — and the rotation is still owed, so it is still staged. Clearing
    the marker before the answer is in loses a rotation whenever the gateway is
    down."""
    state, fire = hooks
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    _stage(state)

    def refuse(_request, timeout=None):
        raise OSError("gateway down")

    monkeypatch.setattr(fire.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(fire, "_detach_and_fire", fire._post_fire)
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(_event())))
    fire.main()

    marker = state.read_marker("conv-1")
    assert marker is not None, "a failed fire threw the rotation away"
    assert marker["state"] == state.STATE_READY, "the marker was left mid-fire"


def test_a_confirmed_rotation_clears_the_marker(hooks, monkeypatch):
    """The other side of requirement 7: once the swap is real the marker must
    go, or the successor's own opening turn rotates it away again — one
    rotation per turn, forever, each from the same stale summary."""
    state, fire = hooks
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    _stage(state)

    class _Response:
        def read(self):
            return json.dumps({"ok": True, "rotated": True}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(fire.urllib.request, "urlopen", lambda *_a, **_k: _Response())
    monkeypatch.setattr(fire, "_detach_and_fire", fire._post_fire)
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(_event())))
    fire.main()

    assert state.read_marker("conv-1") is None


def test_a_conversation_compacted_back_under_the_threshold_drops_the_rotation(
    hooks, monkeypatch, tmp_path
):
    """Requirement 8: /compact reaches no hook and resets the live context.
    Firing afterwards stacks a second summarisation on a conversation with
    room to spare.

    Measured against a real transcript shape, not a stubbed answer. The
    harness reports nothing about a compaction until the *next* assistant
    turn completes, so a check that reads the last reported usage — which is
    every check that runs at UserPromptSubmit — reads the pre-compact figure
    and fires on exactly the case this exists to catch.
    """
    state, fire = hooks
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    _stage(state, forced=False)

    transcript = tmp_path / "compacted.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 10_000_000, "output_tokens": 1}},
        }) + "\n"
        + json.dumps({
            "type": "user", "isCompactSummary": True,
            "message": {"role": "user", "content": "the story so far"},
        }) + "\n"
    )
    event = _event()
    event["transcript_path"] = str(transcript)

    assert _fire(fire, monkeypatch, event) == []
    assert state.read_marker("conv-1") is None, "a rotation nobody needs is still pending"


@pytest.mark.parametrize("module", ["claude_cli", "codex_cli"])
def test_an_oversized_seed_is_trimmed_to_what_exec_can_carry(module):
    """The kernel caps one argv entry at 128 KiB of *bytes*. A carry-forward
    is UTF-8, and a rotation summary quoting a TUI transcript carries
    box-drawing and arrows — so a cap counted in characters passes a seed that
    exec refuses. That failure lands inside the pane, after tmux has returned
    0 and the gateway has written the successor's id onto the row: a rotation
    reported as successful, on a pane that is dead."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import importlib

    harness = importlib.import_module(f"mind_templates.{module}")

    # Comfortably under the cap by characters, well over it by bytes.
    seed = "━" * 119_000
    assert len(seed) < harness.MAX_SEED_BYTES
    assert len(seed.encode("utf-8")) > harness.MAX_SEED_BYTES

    capped = harness._capped_seed(seed)
    assert len(capped.encode("utf-8")) <= harness.MAX_SEED_BYTES
    # The tail is what survives: composition puts the summary and the turns
    # typed during the window last.
    assert capped.endswith("━")


def test_what_is_running_is_recorded_from_the_payload_the_harness_provides(
    tmp_path, monkeypatch
):
    """Requirement 5's input: UserPromptSubmit carries no account of live
    background work, so the Stop and SubagentStop payloads that do are
    snapshotted for it — for a pane, and only for a pane. Every session on
    this host runs this hook, and only a terminal has a rotation to hold."""
    event = json.dumps({
        "session_id": "conv-9",
        "hook_event_name": "SubagentStop",
        "background_tasks": [
            {"id": "x", "type": "subagent", "status": "running", "description": "digging"},
            {"id": "y", "type": "shell", "status": "completed", "command": "ls"},
        ],
    })

    def run(surface: str):
        return subprocess.run(
            [sys.executable, str(HOOKS_DIR / "bg_snapshot.py")],
            input=event, capture_output=True, text=True,
            env={**os.environ, "HIVE_PROJECT_DIR": str(tmp_path),
                 "HIVE_SURFACE": surface},
            timeout=30,
        )

    snapshot_path = tmp_path / "data" / "auto-remember" / "bgtasks-conv-9.json"

    assert run("telegram").returncode == 0
    assert not snapshot_path.exists(), "a chat session wrote a pane's state file"

    result = run("terminal")
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(snapshot_path.read_text())
    assert [t["id"] for t in snapshot["running"]] == ["x"]


def test_a_second_prompt_cannot_start_a_rival_rotation(hooks, monkeypatch):
    """Two prompts queued while the first was waiting would otherwise both
    respawn the same pane onto the same conversation id, racing over one seed
    file — one message lost, and the successor possibly opening on nothing."""
    state, fire = hooks
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    _stage(state)

    assert len(_fire(fire, monkeypatch, _event(prompt="first"))) == 1
    assert _fire(fire, monkeypatch, _event(prompt="second")) == []


def test_a_composition_that_died_does_not_hold_the_next_message(hooks, monkeypatch, tmp_path):
    """The marker goes down before the work, so every way out of composition —
    a failed memory write, an Ollama timeout, a missing mind id — is a way to
    leave it saying "composing" with nothing left to promote it. The fire hook
    would then wait on every later message for a summary never coming."""
    state, _fire_mod = hooks
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    monkeypatch.setenv("OWNER_TYPE", "web")
    monkeypatch.setenv("CLIENT_REF", "term-1")
    monkeypatch.delenv("MIND_ID", raising=False)
    for name in ("rotation_check",):
        sys.modules.pop(name, None)
    import rotation_check

    rotation_check._run({
        "session_id": "conv-1",
        "transcript_path": str(_transcript(tmp_path / "big.jsonl", 10_000_000)),
        "force": True,
    })

    assert state.read_marker("conv-1") is None
    sys.modules.pop("rotation_check", None)


def test_a_session_that_is_not_a_terminal_is_left_alone(hooks, monkeypatch):
    """Only a pane rotates in place. Every other surface finalizes through the
    gateway's own user turn, and a hook firing on one would respawn a pane
    that does not exist."""
    state, fire = hooks
    monkeypatch.setenv("HIVE_SURFACE", "telegram")
    _stage(state)

    assert _fire(fire, monkeypatch, _event()) == []
    assert state.read_marker("conv-1") is not None


def test_a_marker_left_by_a_pane_nobody_returns_to_expires(hooks):
    """A staged rotation is a promise about the user's next message. A pane
    closed overnight has no next message, and firing one on whatever reopens
    it is a respawn nobody asked for."""
    state, _fire_mod = hooks
    _stage(state, sid="conv-old")
    marker = state._read_json(state._marker_path("conv-old"))
    marker["staged_at"] = time.time() - state.MARKER_TTL_SECONDS - 1
    state._write_json(state._marker_path("conv-old"), marker)

    assert state.read_marker("conv-old") is None
    assert not state._marker_path("conv-old").exists()


# ---------------------------------------------------------------------------
# The three ways a rotation used to be composed and then thrown away.
#
# On 2026-08-07/08 this pane armed 17 rotations and fired none. Each of the
# cases below is one of the reasons, measured in rotation.log, and each fails
# the same way: silently, positionally, and only visible as a GPU spinning
# for minutes on a summary nobody can use.
# ---------------------------------------------------------------------------


@pytest.fixture()
def check_module(tmp_path, monkeypatch):
    """``rotation_check`` with its state directory pointed at a temp tree."""
    monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    for name in ("rotation_state", "rotation_check"):
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(str(HOOKS_DIR))
    import rotation_check
    import rotation_state
    yield rotation_check, rotation_state
    for name in ("rotation_state", "rotation_check"):
        sys.modules.pop(name, None)


def _detached(fn) -> dict:
    """Run fn() the way the Stop hook runs its work: forked and setsid'd."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.setsid()
            os.write(write_fd, json.dumps(fn()).encode())
        except BaseException as exc:  # noqa: BLE001 — reported to the parent
            os.write(write_fd, json.dumps({"error": repr(exc)}).encode())
        finally:
            os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd) as handle:
        raw = handle.read()
    os.waitpid(pid, 0)
    return json.loads(raw)


_THRESHOLDS = {
    "THRESHOLD_OPUS": 300000,
    "THRESHOLD_SONNET": 100000,
    "THRESHOLD_TOKENS": 300000,
    "THRESHOLD_NO_1M": 140000,
}


def test_the_detached_hook_uses_the_spawn_model_it_captured_before_forking(
    check_module, monkeypatch
):
    """A pane's threshold has to survive the hook detaching from the harness.

    ``_spawned_model_arg`` finds the ``[1m]`` pin by walking the ppid chain,
    and ``main`` forks and setsid's before any work runs — so asking again
    down there answers "" on a real orphan, which reads as "no extended
    window" and caps a 1M Opus pane at the 140k fallback. That is what armed
    119 rotations the fire hook then dropped for being under *its* 300k.

    The assertion is on the call *count*, not just the value: a value alone
    still passes under lazy probing on a host where the chain happens to
    resolve, which is exactly how this went unnoticed.
    """
    rotation_check, _state = check_module
    calls = {"n": 0}

    def _counted():
        calls["n"] += 1
        return "claude-opus-5[1m]"

    monkeypatch.setattr(rotation_check, "_probe_spawned_model_arg", _counted)
    monkeypatch.setattr(rotation_check, "_SPAWN_MODEL_ARG", None, raising=False)

    rotation_check._spawned_model_arg()          # pre-fork, as main() does
    assert calls["n"] == 1

    def _in_child():
        # Order matters: the threshold lookup is what would re-probe, so the
        # counter has to be read *after* it. Reading it first — as a dict
        # literal does, top to bottom — samples the count before the call
        # that increments it, and the test then passes a full revert.
        threshold = rotation_check._threshold_for_model(
            _THRESHOLDS, "claude-opus-5"
        )
        return {"threshold": threshold, "probe_calls": calls["n"]}

    got = _detached(_in_child)

    assert got.get("error") is None, got
    assert got["probe_calls"] == 1, (
        "the detached child re-probed; on a real orphan that yields '' and "
        "the pane arms at the 140k fallback instead of 300k"
    )
    assert got["threshold"] == 300000


def test_a_pane_without_the_extended_pin_still_arms_at_the_lower_bound(
    check_module, monkeypatch
):
    """The fallback is not dead code. Always answering 300000 would satisfy
    the test above and let a genuine 200k window run to 100% of itself —
    the failure the split threshold exists to prevent."""
    rotation_check, _state = check_module
    monkeypatch.setattr(
        rotation_check, "_probe_spawned_model_arg", lambda: "claude-opus-5"
    )
    monkeypatch.setattr(rotation_check, "_SPAWN_MODEL_ARG", None, raising=False)
    assert rotation_check._threshold_for_model(_THRESHOLDS, "claude-opus-5") == 140000


def test_a_hook_that_lost_the_lock_leaves_the_running_rotation_alone(
    check_module, monkeypatch
):
    """Losing the re-entrancy lock must not delete the winner's marker.

    ``_run`` drops unsettled markers in a ``finally``, and ``_compose_rotation``
    returns having staged nothing when a rotation is already in flight. The
    loser was deleting the marker the winner was still composing for —
    16 times in one day, each one a full Ollama run that armed a rotation the
    fire hook could no longer see.
    """
    rotation_check, state = check_module
    sid = "conv-being-composed"
    state.stage_marker(sid, client_type="web", client_ref="term-1", forced=False)

    monkeypatch.setattr(rotation_check, "_STAGED_HERE", False)   # never staged
    rotation_check._drop_unsettled_marker(sid)

    survivor = state.read_marker(sid)
    assert survivor is not None, "the lock loser deleted the winner's marker"
    assert survivor["state"] == state.STATE_COMPOSING


def test_the_hook_that_staged_it_still_drops_its_own_abandoned_marker(
    check_module, monkeypatch
):
    """The guard must not make every marker permanent. A composition that
    dies partway has to clear its own, or the fire hook holds every later
    message waiting on a summary that is never coming."""
    rotation_check, state = check_module
    sid = "conv-abandoned"
    state.stage_marker(sid, client_type="web", client_ref="term-1", forced=False)

    monkeypatch.setattr(rotation_check, "_STAGED_HERE", True)
    rotation_check._drop_unsettled_marker(sid)

    assert state.read_marker(sid) is None


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


_COMMS = {"COMMS_URL": "http://127.0.0.1:8426", "COMMS_BEARER": "t"}


def test_a_pane_the_gateway_no_longer_holds_composes_nothing(
    check_module, monkeypatch
):
    """``arm-rotation`` asks this after the composition; asking it first is
    the difference between one local GET and six minutes of GPU. Measured
    twice in one day as ``FAIL arm-rotation … "no active session"``, each
    time against a summary that had already been paid for."""
    rotation_check, _state = check_module
    monkeypatch.setattr(
        rotation_check.urllib.request, "urlopen",
        lambda *a, **k: _Resp([{"id": "s1", "is_active": False}]),
    )
    assert rotation_check._comms_has_active_session(
        _COMMS, client_type="web", client_ref="term-1"
    ) is False


def test_a_pane_the_gateway_still_holds_is_allowed_to_compose(
    check_module, monkeypatch
):
    """The inverse, or the gate above passes by refusing everything."""
    rotation_check, _state = check_module
    monkeypatch.setattr(
        rotation_check.urllib.request, "urlopen",
        lambda *a, **k: _Resp([
            {"id": "s1", "is_active": False},
            {"id": "s2", "is_active": True},
        ]),
    )
    assert rotation_check._comms_has_active_session(
        _COMMS, client_type="web", client_ref="term-1"
    ) is True


def test_an_unreachable_gateway_does_not_strand_a_full_pane(
    check_module, monkeypatch
):
    """This gate saves cost; it does not decide correctness. Refusing to
    compose because a health check blipped would leave a pane sitting at
    100% of its window, and ``arm-rotation`` still refuses on its own if the
    session really is gone — so a blip costs at most one composition."""
    rotation_check, _state = check_module

    def _refused(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(rotation_check.urllib.request, "urlopen", _refused)
    assert rotation_check._comms_has_active_session(
        _COMMS, client_type="web", client_ref="term-1"
    ) is True
