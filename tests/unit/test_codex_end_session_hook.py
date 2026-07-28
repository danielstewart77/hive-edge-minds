"""Tests for the model-free Codex end-session command hook."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "codex_end_session_hook.py"
SPEC = importlib.util.spec_from_file_location("codex_end_session_hook", SCRIPT)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def test_unrelated_prompt_is_ignored(monkeypatch):
    monkeypatch.setattr(hook, "schedule_delete", lambda _session_id: pytest.fail())
    assert hook.handle_event({"prompt": "hello"}) is None


@pytest.mark.parametrize("command", ["$end-session", "/end-session", "  $END-SESSION  "])
def test_command_blocks_model_and_schedules_exact_session(monkeypatch, command):
    scheduled = []
    monkeypatch.setattr(hook, "load_env", lambda: None)
    monkeypatch.setattr(hook, "resolve_session", lambda: "session-123")
    monkeypatch.setattr(hook, "schedule_delete", scheduled.append)

    result = hook.handle_event({"prompt": command})

    assert scheduled == ["session-123"]
    assert result == {
        "decision": "block",
        "reason": (
            "Durable memory has been harvested turn by turn. "
            "The session is closing now."
        ),
    }


def test_resolution_failure_blocks_model_but_leaves_session_open(monkeypatch):
    monkeypatch.setattr(hook, "load_env", lambda: None)
    monkeypatch.setattr(
        hook, "resolve_session", lambda: (_ for _ in ()).throw(RuntimeError("none"))
    )
    monkeypatch.setattr(hook, "schedule_delete", lambda _session_id: pytest.fail())

    result = hook.handle_event({"prompt": "$end-session"})

    assert result["decision"] == "block"
    assert "failed safely" in result["reason"]
    assert "remains open" in result["reason"]


def test_resolve_session_matches_codex_thread_only(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-2")
    monkeypatch.setattr(
        hook,
        "gateway_request",
        lambda _path: [
            {"id": "wrong", "harness_sid": "thread-1", "status": "idle"},
            {"id": "closed", "harness_sid": "thread-2", "status": "closed"},
            {"id": "right", "harness_sid": "thread-2", "status": "idle"},
        ],
    )
    assert hook.resolve_session() == "right"


def test_delayed_delete_uses_gateway_delete(monkeypatch):
    calls = []
    monkeypatch.setattr(hook.time, "sleep", lambda delay: calls.append(("sleep", delay)))
    monkeypatch.setattr(
        hook,
        "gateway_request",
        lambda path, method="GET": calls.append((method, path)),
    )

    hook.delayed_delete("session-123", 2.5)

    assert calls == [("sleep", 2.5), ("DELETE", "/sessions/session-123")]


def test_main_emits_codex_block_contract(monkeypatch, capsys):
    monkeypatch.setattr(hook.sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(hook.json, "load", lambda _stream: {"prompt": "$end-session"})
    monkeypatch.setattr(
        hook,
        "handle_event",
        lambda _event: {"decision": "block", "reason": "closing"},
    )

    assert hook.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "decision": "block",
        "reason": "closing",
    }
