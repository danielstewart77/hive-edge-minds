#!/usr/bin/env python3
"""Handle ``$end-session`` before Codex makes a model request.

Codex skills require a model turn, so they cannot run while the provider is
rate-limited. This UserPromptSubmit hook recognizes the exact command, blocks
the model request, and closes the matching Hive Comms session out of band.
Durable memory is already harvested after every completed turn by the Stop
hook; the command itself carries no additional durable content.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


ENV_PATH = Path("/home/daniel/Storage/mordecai/.env")
LOG_PATH = Path("/home/daniel/Storage/mordecai/data/auto-remember/end-session.log")
COMMANDS = {"$end-session", "/end-session"}


def load_env() -> None:
    if not ENV_PATH.is_file():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def gateway_request(path: str, method: str = "GET") -> object:
    token = os.environ.get("COMMS_BEARER_TOKEN", "")
    base_url = os.environ.get("COMMS_URL", "")
    if not token or not base_url:
        raise RuntimeError("gateway credentials are unavailable")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
    return json.loads(body) if body else {}


def resolve_session() -> str:
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if not thread_id:
        raise RuntimeError("Codex thread identity is unavailable")
    sessions = gateway_request("/sessions")
    if not isinstance(sessions, list):
        raise RuntimeError("gateway returned an invalid session list")
    matches = [
        row
        for row in sessions
        if isinstance(row, dict)
        and row.get("harness_sid") == thread_id
        and row.get("status") != "closed"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one live session, found {len(matches)}")
    session_id = matches[0].get("id")
    if not session_id:
        raise RuntimeError("matched session has no identifier")
    return str(session_id)


def schedule_delete(session_id: str, delay: float = 8.0) -> None:
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--delete",
            session_id,
            "--delay",
            str(delay),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def delayed_delete(session_id: str, delay: float) -> None:
    time.sleep(delay)
    gateway_request(f"/sessions/{session_id}", method="DELETE")


def extract_prompt(event: dict) -> str:
    prompt = event.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def handle_event(event: dict) -> dict | None:
    if extract_prompt(event).strip().lower() not in COMMANDS:
        return None
    load_env()
    try:
        session_id = resolve_session()
        schedule_delete(session_id)
    except (RuntimeError, OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        return {
            "decision": "block",
            "reason": f"End-session failed safely: {exc}. The session remains open.",
        }
    return {
        "decision": "block",
        "reason": (
            "Durable memory has been harvested turn by turn. "
            "The session is closing now."
        ),
    }


def log_delete_failure(session_id: str, exc: Exception) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{time.time():.3f} session={session_id} error={exc}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete")
    parser.add_argument("--delay", type=float, default=8.0)
    args = parser.parse_args()
    load_env()
    if args.delete:
        try:
            delayed_delete(args.delete, args.delay)
        except Exception as exc:  # detached worker must preserve diagnostics
            log_delete_failure(args.delete, exc)
            return 1
        return 0

    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        event = {}
    result = handle_event(event)
    if result is not None:
        json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
