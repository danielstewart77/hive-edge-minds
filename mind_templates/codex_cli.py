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
import fcntl
import json
import logging
import os
import pty
import re
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

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

# session_id -> codex thread id, surviving the session state itself and mind
# restarts. Hive-comms is authoritative; the local file is a safety copy for
# a gateway outage during the first terminal attach.
#
# Codex is the one harness that will not take a conversation id it was handed:
# `codex exec` mints its own thread and reports it on the first event, and
# there is no flag to declare one up front. The gateway's conversation id is
# therefore the *session's* identity, not the thread's, and the mapping between
# them can only live here. Keeping it outside `_sessions` means a respawn of
# the same session (idle eviction, a gateway restart) rejoins its thread
# instead of silently starting a second one.
_THREAD_MAP_PATH = CODEX_HOME / "hive-thread-map.json"
_THREAD_MAP_LOCK = threading.Lock()

# session_id -> {"args": [...], "env": {...}} for the pane's provider routing.
# A rotation respawns the pane with no registry in hand, so what spawn_pty
# resolved has to outlive the call that resolved it.
_PTY_PROVIDER: dict[str, dict[str, Any]] = {}


def _load_thread_map() -> dict[str, str]:
    try:
        data = json.loads(_THREAD_MAP_PATH.read_text())
        return {str(k): str(v) for k, v in data.items() if k and v}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


THREADS: dict[str, str] = _load_thread_map()


def _write_thread_map() -> None:
    _THREAD_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _THREAD_MAP_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(THREADS, sort_keys=True))
    os.replace(temp_path, _THREAD_MAP_PATH)


def _remember_thread(session_id: str, thread_id: str) -> None:
    with _THREAD_MAP_LOCK:
        THREADS[session_id] = thread_id
        _write_thread_map()


def _forget_thread(session_id: str) -> None:
    with _THREAD_MAP_LOCK:
        THREADS.pop(session_id, None)
        _write_thread_map()


def _report_thread(session_id: str, thread_id: str) -> None:
    """Tell hive-comms which provider-native thread belongs to the session."""
    base_url = (
        os.environ.get("COMMS_URL") or os.environ.get("HIVEMIND_BROKER_URL", "")
    ).rstrip("/")
    if not base_url:
        log.warning("Cannot report Codex thread for %s: HIVEMIND_BROKER_URL unset", session_id)
        return
    token = os.environ.get("HIVEMIND_BROKER_TOKEN", "")
    request = urllib.request.Request(
        f"{base_url}/sessions/{session_id}/harness-state",
        data=json.dumps({"harness_sid": thread_id}).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"gateway returned HTTP {response.status}")
    except (OSError, urllib.error.URLError, RuntimeError) as exc:
        log.warning("Failed to report Codex thread %s for session %s: %s",
                    thread_id, session_id, exc)


def _report_thread_in_background(session_id: str, thread_id: str) -> None:
    threading.Thread(
        target=_report_thread, args=(session_id, thread_id), daemon=True
    ).start()


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


def _resolve_provider(registry: Any, model: str) -> Any:
    """The registry's Provider for ``model``, or None when unresolvable.

    A mind spawned without a registry (tests, a bare invocation) keeps the
    default provider rather than failing the turn.
    """
    if registry is None or not model:
        return None
    try:
        return registry.get_provider(model)
    except (ValueError, KeyError):
        return None


def _provider_args(provider: Any, mind_name: str = "MIND_NAME") -> list[str]:
    """Codex ``-c`` overrides pointing the CLI at an Ollama-backed provider.

    Codex has no ``ANTHROPIC_BASE_URL`` equivalent: a non-default provider is
    declared as a ``model_providers.<key>`` config block and selected with
    ``model_provider``. That is the whole reason this exists — the claude
    harness gets the same job done through ``provider.env_overrides`` alone,
    so it needs no counterpart.

    Returns [] for every other provider, so an OpenAI-backed mind spawns the
    exact argv it did before.
    """
    if provider is None or getattr(provider, "name", "") != "ollama":
        return []

    env = getattr(provider, "env_overrides", None) or {}
    base_url = str(env.get("OLLAMA_BASE_URL") or env.get("OPENAI_BASE_URL") or "")
    if not base_url:
        # api_base is the anthropic-shaped field; codex speaks the
        # OpenAI-compatible dialect, which Ollama serves under /v1.
        api_base = str(getattr(provider, "api_base", None) or "").rstrip("/")
        base_url = f"{api_base}/v1" if api_base else "http://localhost:11434/v1"
    base_url = base_url.rstrip("/")

    provider_key = f"{mind_name}_ollama"
    args = [
        "-c", f'model_provider="{provider_key}"',
        "-c", f'model_providers.{provider_key}.name="{mind_name.capitalize()} Ollama"',
        "-c", f'model_providers.{provider_key}.base_url="{base_url}"',
    ]
    if env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        # The "ollama" endpoint may really be a metering proxy in front of
        # Ollama, which authenticates by bearer key. Codex only sends one when
        # the provider declares env_key; bare Ollama needs no auth, so the
        # declaration is gated on a key actually being present.
        args += ["-c", f'model_providers.{provider_key}.env_key="OPENAI_API_KEY"']
    return args


def _base_cmd(provider_args: list[str] | None = None) -> list[str]:
    cmd = ["codex", "exec", "--json"]
    if CODEX_PROFILE:
        cmd.extend(["--profile", CODEX_PROFILE])
    cmd.extend(provider_args or [])
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
    harness_sid: str | None = None,
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

    ``resume_sid`` is the gateway's conversation id. Codex cannot adopt it;
    ``harness_sid`` is the provider-native thread persisted by hive-comms.
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

    if harness_sid:
        _remember_thread(session_id, harness_sid)
    state = {
        "system_prompt": full_prompt,
        "thread_id": harness_sid or THREADS.get(session_id),
        "model": model,
        "proc": None,
        # Codex is per-turn: the subprocess is built in send(), so the
        # provider resolved here has to survive until then.
        "provider": _resolve_provider(registry, model),
        "mind_name": mind_name,
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

    provider = state.get("provider")
    provider_args = _provider_args(provider, state.get("mind_name") or "MIND_NAME")

    thread_id = state.get("thread_id")
    if thread_id:
        cmd = _base_cmd(provider_args) + ["resume", thread_id, "-"]
        stdin_content = content
    else:
        cmd = _base_cmd(provider_args) + ["-"]
        stdin_content = f"{state['system_prompt']}\n\n---\n\n{content}"

    if images:
        log.warning("Codex session %s: image input not supported, ignoring", session_id)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(CODEX_HOME)
    # env_key above only names the variable; codex still reads it from the
    # environment, so the provider's overrides have to be on the subprocess.
    if provider is not None:
        env.update(getattr(provider, "env_overrides", None) or {})
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
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    proc_stdin = proc.stdin
    proc_stdout = proc.stdout
    proc_stderr = proc.stderr
    proc_stdin.write(stdin_content.encode())
    await proc_stdin.drain()
    proc_stdin.close()

    # Drain stderr concurrently. send() pipes stderr; if nothing reads it a
    # chatty codex fills the OS pipe buffer and blocks. The tail also feeds
    # the error paths so a dead codex surfaces its real complaint.
    stderr_buf: list[str] = []

    async def _pump_stderr() -> None:
        try:
            async for errline in proc_stderr:
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
        _forget_thread(session_id)
        _report_thread_in_background(session_id, "")

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
                    proc_stdout.readline(), timeout=TURN_IDLE_TIMEOUT
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
                    _remember_thread(session_id, current_thread_id)

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
    _forget_thread(session_id)
    if state is not None:
        await _reap_proc(state.get("proc"))
    log.info("Codex session %s killed", session_id)


# Browser terminal (tmux-backed), mirroring the claude harness's approach:
# the interactive TUI lives in a tmux session keyed by the hive session and
# outlives every viewer, so a browser attach is just a tmux client living in
# a pty of the caller's geometry. A dedicated socket keeps this server's tmux
# options and lifetime isolated from any tmux Daniel runs by hand.
TMUX_SOCKET = "MIND_NAME-terminal"

# Applied ahead of every session creation so they hold from the pane's first
# byte; see the claude harness's implementation.py for the rationale behind
# each option (history-limit/default-terminal are read at pane creation and
# can't be retrofitted; prefix None + escape-time 0 keep tmux out of a TUI
# that wants every keystroke; window-size latest sizes to whichever client
# most recently attached, the only client this server allows).
_TMUX_OPTIONS = [
    ["set", "-g", "exit-empty", "off"],
    ["set", "-g", "status", "off"],
    ["set", "-g", "prefix", "None"],
    ["set", "-g", "escape-time", "0"],
    ["set", "-g", "history-limit", "50000"],
    # `mouse on` is the only thing that makes the scrollback reachable. A
    # phone has no wheel, so the browser tile synthesises SGR wheel reports
    # from a finger drag -- and with mouse off tmux discards them and the
    # pane's app never asked for mouse, so a drag scrolled precisely nothing
    # and the history behind a session was unreadable from a phone. On, the
    # same reports put tmux into copy-mode and pan its history. Taps are
    # unaffected: the tile only ever synthesises wheel buttons, never a
    # press, so tmux is never handed a click to turn into a selection. An
    # app that enables mouse reporting for itself still gets the events.
    ["set", "-g", "mouse", "on"],
    ["set", "-g", "window-size", "latest"],
    ["set", "-g", "destroy-unattached", "off"],
    ["set", "-g", "default-terminal", "tmux-256color"],
    ["set", "-gas", "terminal-features", ",xterm-256color:RGB"],
]

_CLIENT_TERM = "xterm-256color"


def tmux_session_name(session_id: str) -> str:
    """The tmux session that holds this hive session's terminal."""
    return f"MIND_NAME-{session_id}"


def _take_controlling_tty() -> None:
    """Make the pty the child's controlling terminal, in the child — see
    the claude harness's implementation.py for why this is required for
    SIGWINCH (and thus live resize) to reach the attached client at all."""
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _terminal_argv(
    thread_id: str | None, provider_args: list[str] | None = None
) -> list[str]:
    """The interactive `codex` that runs inside the tmux pane.

    Resumes a known thread (`codex resume <id>`) when one exists and has a
    rollout on disk; a fresh terminal launches bare `codex` instead (see
    `_watch_for_new_thread`). `check_for_update_on_startup=false` is
    required, not cosmetic: the
    update-nag screen's Enter default shells out to
    `npm install -g @openai/codex`, which fails inside the container (the
    non-root user has no write access to the global npm dir) and kills
    codex with exit status 243 before it is ever usable.

    A rotation's carry-forward rides in as codex's positional opening turn
    (this harness has no ``--append-system-prompt``), but not from here —
    see ``_seeded_pane_command``.
    """
    cmd = ["codex"]
    if thread_id:
        cmd.extend(["resume", thread_id])
    if CODEX_PROFILE:
        cmd.extend(["--profile", CODEX_PROFILE])
    cmd.extend(provider_args or [])
    cmd.extend([
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "-c", "check_for_update_on_startup=false",
    ])
    return cmd


# A single argv entry cannot exceed MAX_ARG_STRLEN (32 pages, 128 KiB on
# Linux); exec fails outright above it. The seed reaches codex as one
# argument however it is delivered, so it is capped short of that.
MAX_SEED_CHARS = 120_000


def _capped_seed(system_prompt: str) -> str:
    """Trim an oversized carry-forward to what exec can actually carry.

    The tail is what survives: composition puts the rotation summary and the
    turns typed during the window last, and those are the ones the successor
    has to pick the conversation up from.
    """
    if len(system_prompt) <= MAX_SEED_CHARS:
        return system_prompt
    log.warning("Rotation seed of %d chars exceeds the %d-char exec limit — "
                "keeping the tail", len(system_prompt), MAX_SEED_CHARS)
    return ("[earlier context omitted: the carry-forward was too large to "
            "pass to the harness]\n\n" + system_prompt[-MAX_SEED_CHARS:])


def _seeded_pane_command(
    argv: list[str], system_prompt: str, seed_key: str
) -> list[str]:
    """Hand the pane its carry-forward without putting it in the command.

    A rotation seed is a composed prompt — soul, recent memory, the summary,
    the turns typed during the window — and tmux rejects a `respawn-pane`
    whose command exceeds its own length limit with "command too long". So
    the seed goes to a file and the pane reads it back through a one-line
    shell that then `exec`s codex with it as the opening turn: the tmux
    command stays short regardless of how much context is being carried.
    """
    if not system_prompt:
        return argv

    system_prompt = _capped_seed(system_prompt)
    seed_dir = CODEX_HOME / "rotation-seeds"
    seed_dir.mkdir(parents=True, exist_ok=True)
    seed_file = seed_dir / f"{seed_key}.txt"
    seed_file.write_text(system_prompt)
    quoted_seed = shlex.quote(str(seed_file))
    harness = " ".join(shlex.quote(arg) for arg in argv)
    # Read then delete: the seed is one conversation's memory sitting in a
    # file, and it is only ever needed once, at exec time.
    return [
        "/bin/sh", "-c",
        f'seed=$(cat {quoted_seed}); rm -f {quoted_seed}; exec {harness} "$seed"',
    ]


def _tmux(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", TMUX_SOCKET, *args],
        capture_output=True, text=True, env=env,
    )


def pty_session_alive(session_id: str) -> bool:
    """True while this session's terminal process is still running.

    The `=` is exact-match: tmux targets are prefix-matched by default, so a
    session whose id is a prefix of another's would answer for it.
    """
    return _tmux("has-session", "-t", f"={tmux_session_name(session_id)}").returncode == 0


def kill_pty_session(session_id: str) -> bool:
    """End the terminal for good. True if there was one to end."""
    name = tmux_session_name(session_id)
    _PTY_PROVIDER.pop(session_id, None)
    if _tmux("kill-session", "-t", f"={name}").returncode != 0:
        return False
    log.info("Killed tmux session %s", name)
    return True


_ROLLOUT_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


def _existing_rollout_paths() -> set[Path]:
    sessions_dir = CODEX_HOME / "sessions"
    if not sessions_dir.exists():
        return set()
    return set(sessions_dir.rglob("*.jsonl"))


def _rollout_exists(thread_id: str) -> bool:
    """A harness_sid is only resumable if its rollout is on *this* CODEX_HOME.

    A thread id can outlive the container that minted it — a redeploy onto a
    fresh volume, a migration to a new host — while hive-comms and the disk
    safety copy still point at it. `codex resume` on a missing rollout dies
    within a second of tmux starting it, which reads identically to a hung
    terminal. Check before trusting either source.
    """
    return any(
        _ROLLOUT_UUID_RE.search(path.name)
        and _ROLLOUT_UUID_RE.search(path.name).group(1).lower() == thread_id.lower()
        for path in _existing_rollout_paths()
    )


def _watch_for_new_thread(session_id: str, before: set[Path]) -> None:
    """A fresh terminal starts bare `codex` with no thread id yet: app-server's
    thread/start mints one without persisting a rollout, and codex only
    writes that file once a real turn begins — there is nothing on disk to
    resume before the user's first message. Poll for the rollout codex
    itself creates and report it, so a later reattach (after the tmux
    session has died) resumes the real conversation instead of starting a
    new one. Gives up once the tmux session ends with nothing ever typed.
    """
    name = tmux_session_name(session_id)
    while _tmux("has-session", "-t", name).returncode == 0:
        new = _existing_rollout_paths() - before
        for path in new:
            match = _ROLLOUT_UUID_RE.search(path.name)
            if match:
                _remember_thread(session_id, match.group(1))
                _report_thread(session_id, match.group(1))
                return
        time.sleep(1.0)


def _watch_for_new_thread_in_background(session_id: str, before: set[Path]) -> None:
    threading.Thread(
        target=_watch_for_new_thread, args=(session_id, before), daemon=True
    ).start()


def _session_env(
    client_ref: str | None, owner_type: str | None, owner_ref: str | None
) -> dict[str, str]:
    """Per-session env the hooks inside the pane need to reach the gateway.

    A tmux pane inherits from the tmux *server*, which was started before
    this session existed and knows nothing about it, so these ride on `-e`
    per pane. Without CLIENT_REF the Stop hook's rotation check bails on
    every fire and a terminal conversation never rotates at all — it just
    grows until the harness's own compaction is the only thing left.
    """
    env: dict[str, str] = {}
    if client_ref:
        env["CLIENT_REF"] = client_ref
    if owner_type:
        env["OWNER_TYPE"] = owner_type
    if owner_ref:
        env["OWNER_REF"] = owner_ref
    return env


def rotate_pty_session(
    session_id: str,
    new_claude_sid: str,
    model: str = "",
    system_prompt: str = "",
    client_ref: str | None = None,
    owner_type: str | None = None,
    owner_ref: str | None = None,
    **kwargs: Any,
) -> bool:
    """Start a fresh codex thread in a live terminal, in place.

    A rotation replaces the *conversation*, not the session and not the
    terminal: the hive session id is permanent, so nothing is renamed. The
    pane's process is respawned onto a fresh codex thread and the attached
    client — and therefore the pty, the websocket and the browser tile above
    it — is never disturbed.

    Codex mints its own thread ids and cannot be handed one, so the new
    thread starts bare and the same background watcher used by a fresh
    terminal reports the id codex writes on the first turn. The rotation
    carry-forward rides in as codex's opening prompt, which is the only
    channel this harness has for it. Returns False when there was no live
    terminal to rotate.

    ``client_ref``/``owner_type``/``owner_ref`` are re-stamped on the new
    process: ``respawn-pane`` builds the pane's environment from the tmux
    server's, which never had them, so a rotation that dropped them would
    hand back a pane whose Stop hook can no longer arm the *next* rotation.
    """
    del model, new_claude_sid, kwargs  # symmetry with the claude harness

    name = tmux_session_name(session_id)

    if not pty_session_alive(session_id):
        log.info("No live terminal for session %s — nothing to rotate in place",
                 session_id)
        return False

    # The old thread id must go: it belongs to the conversation being
    # replaced, and a later reattach that resumed it would undo the rotation.
    _forget_thread(session_id)

    before = _existing_rollout_paths()
    # respawn-pane builds the pane's environment from the tmux server's, which
    # never had the provider routing either — re-stamp it alongside the
    # session attribution or the rotated pane falls back to OpenAI.
    pty_provider = _PTY_PROVIDER.get(session_id) or {}
    cmd = _seeded_pane_command(
        _terminal_argv(None, pty_provider.get("args")),
        system_prompt,
        session_id,
    )

    env = os.environ.copy()
    overrides = {"CODEX_HOME": str(CODEX_HOME), "HIVE_SURFACE": "terminal"}
    overrides.update(pty_provider.get("env") or {})
    overrides.update(_session_env(client_ref, owner_type, owner_ref))
    env.update(overrides)

    # respawn-pane -k replaces the pane's process without touching the
    # window, the session, or any attached client.
    # No `=` exact-match prefix here: that syntax is for session targets, and
    # tmux parses a pane target differently.
    args = ["respawn-pane", "-k", "-t", name, "-c", str(PROJECT_DIR)]
    for key, value in overrides.items():
        args.extend(["-e", f"{key}={value}"])
    args.append("--")
    args.extend(cmd)

    result = _tmux(*args, env=env)
    if result.returncode != 0:
        raise RuntimeError(
            f"tmux refused to respawn the pane for session {session_id}: "
            f"{(result.stderr or result.stdout).strip()}"
        )

    _watch_for_new_thread_in_background(session_id, before)
    log.info("Rotated the conversation in terminal %s (seed=%d chars)",
             name, len(system_prompt))
    return True


def spawn_pty(
    session_id: str,
    model: str,
    resume_sid: str | None = None,
    harness_sid: str | None = None,
    mcp_config: str = "",
    registry: Any = None,
    config_obj: Any = None,
    mind_id: str = "MIND_NAME",
    mind_name: str = "MIND_NAME",
    cols: int = 80,
    rows: int = 24,
    client_ref: str | None = None,
    owner_type: str | None = None,
    owner_ref: str | None = None,
    **kwargs: Any,
) -> tuple[subprocess.Popen, int]:
    """Attach a pty to this session's interactive `codex`, starting it if needed.

    Mirrors the claude harness's tmux-backed terminal: the TUI lives in a
    tmux session named for the hive session and outlives every viewer; what
    this returns is a tmux *client* running in a pty of the caller's
    geometry. Calling it again for a session that already has a terminal
    attaches a second client to the same `codex` rather than starting a
    rival one.

    ``resume_sid`` is the gateway conversation id. ``harness_sid`` is the
    separately persisted Codex thread id. A fresh terminal has neither: it
    launches bare `codex`, and a background watcher reports the thread id
    once codex creates one on the first real turn. Every later reattach
    launches with ``codex resume <thread_id>`` — unless that thread's
    rollout no longer exists under this ``CODEX_HOME`` (a migration or a
    redeploy onto a fresh volume), in which case it is discarded and
    treated as fresh.
    """
    del mind_id, kwargs, mcp_config, config_obj  # unused

    # A rotation respawns this pane without a registry in hand, so the
    # provider routing is resolved once here and kept for the pane's life.
    provider = _resolve_provider(registry, model)
    provider_args = _provider_args(provider, mind_name)
    provider_env = (getattr(provider, "env_overrides", None) or {}) if provider else {}
    _PTY_PROVIDER[session_id] = {"args": provider_args, "env": provider_env}

    if not resume_sid:
        raise ValueError(
            f"spawn_pty for session {session_id} got no conversation id — "
            "the gateway mints one at session creation and must pass it"
        )

    if harness_sid:
        _remember_thread(session_id, harness_sid)

    name = tmux_session_name(session_id)
    env = os.environ.copy()
    overrides = {
        "CODEX_HOME": str(CODEX_HOME),
        # A pty spawn is the web terminal by definition — no gateway
        # derivation needed. surface_inject.sh reads this to tell the model
        # which surface each turn arrived on.
        "HIVE_SURFACE": "terminal",
    }
    overrides.update(provider_env)
    overrides.update(_session_env(client_ref, owner_type, owner_ref))
    env.update(overrides)

    if not pty_session_alive(session_id):
        thread_id = harness_sid or THREADS.get(session_id)
        if thread_id and not _rollout_exists(thread_id):
            # A thread id outlived its rollout — a redeploy onto a fresh
            # volume, a migration to a new host. hive-comms and the disk
            # safety copy still point at it, but `codex resume` on it dies
            # within a second of tmux starting it. Treat it as if this
            # terminal had never had a thread.
            log.warning(
                "Discarding stale thread %s for session %s — no matching "
                "rollout under %s", thread_id, session_id, CODEX_HOME,
            )
            _forget_thread(session_id)
            thread_id = None
        elif thread_id and not harness_sid:
            # Backfill hive-comms from the disk safety copy after an upgrade
            # or a gateway outage during the original thread report.
            _report_thread(session_id, thread_id)
        # A brand-new terminal has no thread yet: launch bare `codex` and
        # let it mint its own on the user's first turn (see
        # _watch_for_new_thread for why this can't be pre-created).
        before = _existing_rollout_paths() if not thread_id else set()
        cmd = _terminal_argv(thread_id, provider_args)

        args: list[str] = []
        for option in _TMUX_OPTIONS:
            args.extend([*option, ";"])
        args.extend(["new-session", "-d", "-s", name, "-c", str(PROJECT_DIR),
                     "-x", str(cols), "-y", str(rows)])
        for key, value in overrides.items():
            args.extend(["-e", f"{key}={value}"])
        args.append("--")
        args.extend(cmd)

        result = _tmux(*args, env=env)
        if result.returncode != 0:
            raise RuntimeError(
                f"tmux refused to start the terminal for session {session_id}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        log.info("Started tmux session %s (thread=%s)", name, thread_id or "new")
        if not thread_id:
            _watch_for_new_thread_in_background(session_id, before)

    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    client_env = dict(env, TERM=_CLIENT_TERM)
    proc = subprocess.Popen(
        ["tmux", "-L", TMUX_SOCKET, "attach-session", "-d", "-t", name],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=client_env,
        cwd=str(PROJECT_DIR),
        preexec_fn=_take_controlling_tty,
        close_fds=True,
    )
    os.close(slave_fd)
    log.info("Attached tmux client to %s for session %s (pid=%d, %dx%d)",
             name, session_id, proc.pid, cols, rows)
    return proc, master_fd
