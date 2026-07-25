"""CLI entry point for a mind's wake-word app."""

from __future__ import annotations

import argparse
import asyncio
import json

from voice.wake_word_app import run_microphone_wake_word_app, run_stdin_wake_word_app
from voice.wake_word_window import launch_wake_word_window


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a mind wake-word app")
    parser.add_argument(
        "--mode",
        choices=("stdin", "microphone", "ui"),
        default="microphone",
        help="Transcript source to use",
    )
    args = parser.parse_args()

    if args.mode == "ui":
        launch_wake_word_window()
        return

    runner = run_microphone_wake_word_app if args.mode == "microphone" else run_stdin_wake_word_app
    results = asyncio.run(runner())
    print(json.dumps([result.asdict() for result in results], indent=2))


if __name__ == "__main__":
    main()
