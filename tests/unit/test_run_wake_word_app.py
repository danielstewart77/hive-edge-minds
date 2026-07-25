"""Tests for the wake-word CLI entry point."""

from __future__ import annotations

import json

from voice import run_wake_word_app


def test_main_defaults_to_microphone_mode(monkeypatch, capsys) -> None:
    async def fake_microphone() -> list[object]:
        class Result:
            def asdict(self) -> dict[str, str]:
                return {"mode": "microphone"}

        return [Result()]

    async def fake_stdin() -> list[object]:
        raise AssertionError("stdin runner should not be used by default")

    monkeypatch.setattr(run_wake_word_app, "run_microphone_wake_word_app", fake_microphone)
    monkeypatch.setattr(run_wake_word_app, "run_stdin_wake_word_app", fake_stdin)
    monkeypatch.setattr("sys.argv", ["run_wake_word_app.py"])

    run_wake_word_app.main()

    output = capsys.readouterr().out
    assert json.loads(output) == [{"mode": "microphone"}]


def test_main_allows_stdin_mode(monkeypatch, capsys) -> None:
    async def fake_microphone() -> list[object]:
        raise AssertionError("microphone runner should not be used in stdin mode")

    async def fake_stdin() -> list[object]:
        class Result:
            def asdict(self) -> dict[str, str]:
                return {"mode": "stdin"}

        return [Result()]

    monkeypatch.setattr(run_wake_word_app, "run_microphone_wake_word_app", fake_microphone)
    monkeypatch.setattr(run_wake_word_app, "run_stdin_wake_word_app", fake_stdin)
    monkeypatch.setattr("sys.argv", ["run_wake_word_app.py", "--mode", "stdin"])

    run_wake_word_app.main()

    output = capsys.readouterr().out
    assert json.loads(output) == [{"mode": "stdin"}]


def test_main_allows_ui_mode(monkeypatch) -> None:
    launched: list[bool] = []

    async def fake_microphone() -> list[object]:
        raise AssertionError("microphone runner should not be used in ui mode")

    async def fake_stdin() -> list[object]:
        raise AssertionError("stdin runner should not be used in ui mode")

    monkeypatch.setattr(run_wake_word_app, "run_microphone_wake_word_app", fake_microphone)
    monkeypatch.setattr(run_wake_word_app, "run_stdin_wake_word_app", fake_stdin)
    monkeypatch.setattr(run_wake_word_app, "launch_wake_word_window", lambda: launched.append(True))
    monkeypatch.setattr("sys.argv", ["run_wake_word_app.py", "--mode", "ui"])

    run_wake_word_app.main()

    assert launched == [True]
