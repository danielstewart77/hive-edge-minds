"""The listener launcher must land in UI mode with streams file-bound."""

from __future__ import annotations

import sys

import launch_listener


def test_launcher_runs_ui_mode_with_env_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(launch_listener, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("WAKE_WORD_DISPLAY_NAME=TestMind\n")

    seen = {}

    def fake_run_main():
        seen["argv"] = list(sys.argv)
        import os
        seen["display_name"] = os.getenv("WAKE_WORD_DISPLAY_NAME")

    monkeypatch.setattr("voice.run_wake_word_app.main", fake_run_main)
    orig_out, orig_err = sys.stdout, sys.stderr
    try:
        launch_listener.main()
    finally:
        sys.stdout, sys.stderr = orig_out, orig_err

    assert seen["argv"][1:] == ["--mode", "ui"]
    assert seen["display_name"] == "TestMind"
    assert (tmp_path / "data" / "wake-word" / "listener.log").exists()
