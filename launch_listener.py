"""Windows Task Scheduler entry point for the wake-word listener window.

Runs under ``pythonw.exe`` (no console in the kid's session), so stdout
and stderr are rebound to a logfile before anything imports, then the
``.env`` is loaded and the desktop window launched.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    log_dir = ROOT / "data" / "wake-word"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "listener.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = log_file
    sys.stderr = log_file

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    sys.argv = [sys.argv[0], "--mode", "ui"]
    from voice.run_wake_word_app import main as run_main

    run_main()


if __name__ == "__main__":
    main()
