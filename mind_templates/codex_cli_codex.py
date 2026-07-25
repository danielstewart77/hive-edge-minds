"""Codex CLI harness adapter template.

Thin spawn/send/kill seam loaded by the shared ``mind_server.py``. A Claude
mind drives a long-lived ``claude --stream-json`` subprocess; a Codex mind
drives ``codex exec --json`` one subprocess per turn and stores the codex
``thread_id`` for conversation resumption.

The system prompt is composed by hive-comms and shipped as
``system_prompt_blocks`` in the spawn payload — this module composes nothing
locally. ``mind_server`` detects the ``send`` coroutine and the
``session_id``-taking ``kill`` and routes the codex per-turn path
automatically.

Process-tree lessons baked in: POSIX spawns with ``start_new_session=True``
so ``killpg`` reaps the node wrapper and its rust child together instead of
orphaning the child to PID 1. Windows spawns with CREATE_NO_WINDOW — codex
gets a hidden console its children inherit, so no console CTRL event from
the bash/curl/jq hook subprocesses can kill the turn mid-flight
(STATUS_CONTROL_C_EXIT surfacing as random empty replies), and no
console-subsystem child pops a visible conhost window on the logged-in
user's desktop (which DETACHED_PROCESS caused). See ``_spawn_isolation``.

A dirty ``thread_id`` is reset on failed/incomplete turns so the next turn
never resumes a broken thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, AsyncGenerator

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]

# Codex's config/state root. Holds config.toml (with the hook registrations),
# auth.json, and the sessions/ rollout transcripts. Shared with the host
# user's interactive codex — one home, one auth token, no staleness. The
# CODEX_HOME env var (set in .env) is authoritative; the default is a fallback.
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))

# Optional codex config profile (a [profiles.<name>] block in CODEX_HOME's
# config.toml — e.g. one that routes through a local inference proxy). Empty
# means codex's defaults.
CODEX_PROFILE = os.environ.get("CODEX_PROFILE", "").strip()

# No output for this many seconds means the turn wedged before turn.completed;
# we reap and surface an error instead of blocking the session forever.
TURN_IDLE_TIMEOUT = float(os.environ.get("CODEX_TURN_IDLE_TIMEOUT", "300"))

# session_id -> {"system_prompt": str, "thread_id": str | None, "model": str,
#                "proc": Process | None, "client_ref"/"owner_type"/"owner_ref"}
_sessions: dict[str, dict] = {}

# session_id -> codex thread id, surviving the session state itself.
#
# Codex is the one harness that will not take a conversation id it was handed:
# `codex exec` mints its own thread and reports it on the first event, and
# there is no flag to declare one up front. The gateway's conversation id is
# therefore the *session's* identity, not the thread's, and the mapping between
# them can only live here. Keeping it outside `_sessions` means a respawn of
# the same session (idle eviction, a gateway restart) rejoins its thread
# instead of silently starting a second one.
THREADS: dict[str, str] = {}


def _spawn_isolation() -> dict:
    """Platform-correct subprocess isolation for the codex tree.

    Windows: CREATE_NO_WINDOW gives codex a hidden console its children
    inherit — no console CTRL event can kill the turn, and no child pops a
    visible conhost window. CREATE_NEW_PROCESS_GROUP keeps ctrl-c events in
    the parent's group away from the turn. POSIX: start_new_session puts the
    node wrapper and its rust child in one session so killpg reaps the tree.
    """
    if os.name == "nt":
        # getattr fallbacks keep this callable on POSIX for tests; the
        # attrs exist for real on Windows.
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": no_window | new_group}
    return {"start_new_session": True}


def _base_cmd() -> list[str]:
    cmd = ["codex", "exec", "--json"]
    if CODEX_PROFILE:
        cmd.extend(["--profile", CODEX_PROFILE])
    # `--dangerously-bypass-hook-trust` is required for the Stop/UserPromptSubmit
    # hooks (capture, auto_remember, rotation, contextual_retrieval) to fire under
    # headless `codex exec`. Without it Codex enables hooks via config but refuses
    # to run them untrusted in automation, silently skipping the whole memory
    # pipeline. These are the mind's own authored hooks — the exact "automation
    # that vets its own hook sources" case the flag is intended for.
    cmd.extend(["--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust"])
    return cmd


def extract_assistant_texts(event: dict) -> list[str]:
    """Return user-visible assistant text from a raw codex stream event.

    Codex emits one JSON object per line under ``codex exec --json``. The only
    events that carry text a human should see are ``item.completed`` events
    whose ``item.type`` is ``agent_message``; their ``text`` field is the
    assistant's reply. Everything else — ``thread.started``, ``turn.completed``,
    tool-call items (``command_execution``, ``file_change``, reasoning, etc.) —
    is structural and deterministically produces no text here.
    """
    if event.get("type") != "item.completed":
        return []
    item = event.get("item") or {}
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        return []
    text = item.get("text")
    if not text:
        return []
    return [text]


def _assistant_text_event(text: str) -> dict:
    """Wrap a plain string as an assistant-visible event so failure detail rides
    the same channel as a normal reply and reaches the surface."""
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _humanize_codex_error(raw: str) -> str:
    """Map known codex failure signatures to actionable guidance, preserving the
    raw detail so nothing is hidden."""
    low = (raw or "").lower()
    if (
        "could not be refreshed" in low
        or "log out and sign in" in low
        or ("access token" in low and ("expired" in low or "invalid" in low))
    ):
        return (
            "Codex authentication has expired: the access token could not be "
            "refreshed. Re-run codex login (device-auth) to restore the session. "
            f"[codex: {raw}]"
        )
    if "usage limit" in low or "rate limit" in low or "quota" in low:
        return f"Codex usage or rate limit reached: {raw}"
    return f"Codex turn failed: {raw}"


async def _reap_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Kill the codex subprocess tree and wait for it to exit.

    codex is a node wrapper that spawns a rust binary as its child. On POSIX,
    killing only the node parent orphans the rust child to PID 1 (us);
    start_new_session=True puts both in one process group so killpg takes
    them down together. Windows has no process groups in that sense — kill
    the parent and let the job die with it. Safe when proc is None or
    already exited.
    """
    if proc is None or proc.returncode is not None:
        return
    if os.name == "nt":
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        log.warning("codex pid %s did not exit within 5s of SIGKILL", proc.pid)


async def spawn(
    session_id: str,
    model: str,
    autopilot: bool = False,
    resume_sid: str | None = None,
    surface_prompt: str | None = None,
    allowed_directories: list[str] | None = None,
    soul_file: Path | None = None,
    mind_id: str = "MIND_NAME",
    mind_name: str = "MIND_NAME",
    system_prompt_blocks: str = "",
    mcp_config: str = "",
    registry: Any = None,
    config_obj: Any = None,
    is_group_session: bool = False,
    logger: logging.Logger | None = None,
    client_ref: str | None = None,
    **kwargs: Any,
) -> dict:
    """Initialise codex session state. No subprocess yet — codex is per-turn.

    Returns the state dict (stored as the session's ``proc`` slot by
    ``mind_server``). The actual codex subprocess is spawned in ``send``.

    ``resume_sid`` is the gateway's conversation id. Codex cannot adopt it —
    see THREADS — so it is never passed to the CLI; this session's thread is
    whatever codex minted for it, if it has spoken at all.
    """
    _log = logger or log

    # NS-composed blocks (soul + standing + recent + session-memory). The
    # surface prompt (e.g. the Telegram surface) appends after a blank line.
    if system_prompt_blocks and surface_prompt:
        full_prompt = f"{system_prompt_blocks}\n\n{surface_prompt}"
    elif surface_prompt:
        full_prompt = surface_prompt
    else:
        full_prompt = system_prompt_blocks

    state = {
        "system_prompt": full_prompt,
        "thread_id": THREADS.get(session_id),
        "model": model,
        "proc": None,
        "client_ref": client_ref or "",
        "owner_type": kwargs.get("owner_type") or "",
        "owner_ref": kwargs.get("owner_ref") or "",
        "is_group_session": is_group_session,
    }
    _sessions[session_id] = state
    _log.info("Codex session %s initialised (model=%s, conversation=%s, thread=%s)",
              session_id, model, resume_sid or "none",
              THREADS.get(session_id) or "new")
    return state


async def send(
    session_id: str,
    content: str,
    images: list[dict] | None = None,
    db: Any = None,
) -> AsyncGenerator[dict, None]:
    """Spawn one codex turn and yield assistant + result events.

    First turn folds the system prompt into stdin; resumed turns send only
    the user message and let codex re-hydrate the thread by ``thread_id``.
    """
    state = _sessions.get(session_id)
    if state is None:
        log.error("No state for codex session %s", session_id)
        yield {"type": "result", "is_error": True}
        return

    thread_id = state.get("thread_id")
    if thread_id:
        cmd = _base_cmd() + ["resume", thread_id, "-"]
        stdin_content = content
    else:
        cmd = _base_cmd() + ["-"]
        stdin_content = f"{state['system_prompt']}\n\n---\n\n{content}"

    if images:
        log.warning("Codex session %s: image input not supported, ignoring", session_id)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(CODEX_HOME)
    # Per-session attribution for the Stop hooks (rotation_check, etc.).
    if state.get("client_ref"):
        env["HIVEMIND_CLIENT_REF"] = state["client_ref"]
        env["CLIENT_REF"] = state["client_ref"]
    if state.get("owner_type"):
        env["OWNER_TYPE"] = state["owner_type"]
    if state.get("owner_ref"):
        env["OWNER_REF"] = state["owner_ref"]
    if state.get("is_group_session"):
        env["HIVEMIND_GROUP_SESSION"] = "1"

    log.info("Codex session %s: spawning turn (thread=%s)", session_id, thread_id or "new")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
        env=env,
        cwd=str(PROJECT_DIR),
        **_spawn_isolation(),
    )
    state["proc"] = proc
    proc.stdin.write(stdin_content.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    # Drain stderr concurrently. send() pipes stderr; if nothing reads it a
    # chatty codex fills the OS pipe buffer and blocks. The tail also feeds
    # the error paths so a dead codex surfaces its real complaint.
    stderr_buf: list[str] = []

    async def _pump_stderr() -> None:
        try:
            async for errline in proc.stderr:
                stderr_buf.append(errline.decode(errors="replace"))
                if len(stderr_buf) > 500:
                    del stderr_buf[0]
        except Exception:
            pass

    stderr_task = asyncio.ensure_future(_pump_stderr())

    def _stderr_tail() -> str:
        return "".join(stderr_buf)[-3000:].strip()

    def _reset_thread() -> None:
        # Don't resume into a thread codex left with an unanswered turn, or
        # the next message gets this turn's response (one turn behind).
        state["thread_id"] = None
        THREADS.pop(session_id, None)

    current_thread_id = thread_id
    # Turn watchdog: codex exec --json streams an event per item as it works,
    # so even a long productive turn emits output steadily. Total silence for
    # TURN_IDLE_TIMEOUT seconds means the turn wedged before turn.completed;
    # without this the readline below blocks forever, the session never
    # frees, and the user gets dead air.
    try:
        while True:
            try:
                raw_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=TURN_IDLE_TIMEOUT
                )
            except asyncio.TimeoutError:
                log.error(
                    "Codex session %s: no output for %ss; turn hung, reaping",
                    session_id, TURN_IDLE_TIMEOUT,
                )
                _reset_thread()
                message = _humanize_codex_error(
                    f"no output for {TURN_IDLE_TIMEOUT:.0f}s; turn reaped. "
                    f"{_stderr_tail()}".strip()
                )
                yield _assistant_text_event(message)
                yield {"type": "result", "is_error": True, "result": message}
                return
            if not raw_line:
                break
            line = raw_line.decode().strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "thread.started":
                current_thread_id = event.get("thread_id")
                state["thread_id"] = current_thread_id
                if current_thread_id:
                    THREADS[session_id] = current_thread_id

            elif etype == "item.completed":
                for text in extract_assistant_texts(event):
                    yield {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": text}],
                        },
                    }

            elif etype == "turn.completed":
                await proc.wait()
                yield {
                    "type": "result",
                    "session_id": current_thread_id,
                    "stop_reason": "end_turn",
                    "is_error": False,
                }
                return

            elif etype == "turn.failed":
                error_msg = event.get("error", {}).get("message", "Unknown error")
                log.error("Codex session %s: turn failed: %s", session_id, error_msg)
                _reset_thread()
                await proc.wait()
                stderr_txt = _stderr_tail()
                detail = f"{error_msg}\n{stderr_txt}".strip() if stderr_txt else error_msg
                message = _humanize_codex_error(detail)
                # Surface the real reason as text instead of a bare error flag.
                yield _assistant_text_event(message)
                yield {"type": "result", "is_error": True, "result": message}
                return

        # Stream ended without turn.completed/failed — incomplete. Same reason:
        # don't leave thread_id dirty or the next turn resumes a broken thread.
        _reset_thread()
        await proc.wait()
        detail = _stderr_tail() or (
            f"codex exited with code {proc.returncode} and produced no output"
        )
        log.error(
            "Codex session %s: stream ended before turn.completed (rc=%s): %s",
            session_id, proc.returncode, detail,
        )
        message = _humanize_codex_error(detail)
        yield _assistant_text_event(message)
        yield {"type": "result", "session_id": current_thread_id,
               "is_error": True, "result": message}
    finally:
        stderr_task.cancel()
        await _reap_proc(state.get("proc"))
        state["proc"] = None


async def kill(session_id: str) -> None:
    """Reap any in-flight codex subprocess and drop the session state."""
    state = _sessions.pop(session_id, None)
    THREADS.pop(session_id, None)
    if state is not None:
        await _reap_proc(state.get("proc"))
    log.info("Codex session %s killed", session_id)
