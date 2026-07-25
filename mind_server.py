"""Minimal mind container server.

Runs inside each mind's container. Manages the harness subprocess
(claude CLI, codex CLI, SDK) via the mind's implementation.py.

Does NOT contain: broker, mind registry, secret storage,
session database, or any nervous system component. Sessions are
tracked in-memory only.
"""

import asyncio
import fcntl
import importlib
import json
import logging
import os
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mind-server")

MIND_ID = os.environ.get("MIND_ID")
if not MIND_ID:
    log.error("MIND_ID environment variable is required")
    sys.exit(1)

MIND_NAME = os.environ.get("MIND_NAME")
if not MIND_NAME:
    log.error("MIND_NAME environment variable is required")
    sys.exit(1)

PROJECT_DIR = Path(__file__).resolve().parent

# Load this mind's implementation
try:
    impl = importlib.import_module(f"minds.{MIND_NAME}.implementation")
    log.info("Loaded implementation for mind: %s (%s)", MIND_NAME, MIND_ID)
except ImportError:
    log.error("No implementation found for mind: %s", MIND_NAME)
    sys.exit(1)

app = FastAPI(title=f"Mind Server: {MIND_ID}")

# In-memory session tracking — no database
_sessions: dict[str, dict] = {}  # session_id -> {"proc": process, "model": str, ...}

# NS gateway URL — for secrets API calls
_NS_URL = os.environ.get("HIVE_MIND_SERVER_URL", "http://server:8420")

# Per-mind config directory (tmpfs, writable)
_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/home/hivemind/.claude-config"))

# Host credentials (read-only mount from host's ~/.claude)
_HOST_CREDS = Path("/home/hivemind/.host-claude/.credentials.json")

# Mind-specific skills source (from minds/<name>/.claude/ in the project)
_MIND_SKILLS_SRC = PROJECT_DIR / "minds" / MIND_NAME / ".claude"


def _setup_config_dir():
    """Set up the per-mind CLAUDE_CONFIG_DIR.

    The config dir is bind-mounted from minds/<name>/.claude/ (read-write).
    Skills and agents are already there. We only need to copy the host's
    auth credentials into it.
    """
    import shutil

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(_CONFIG_DIR)

    # Copy host credentials for auth
    target_creds = _CONFIG_DIR / ".credentials.json"
    if _HOST_CREDS.exists():
        if not target_creds.exists() or _HOST_CREDS.stat().st_mtime > target_creds.stat().st_mtime:
            shutil.copy2(str(_HOST_CREDS), str(target_creds))
            target_creds.chmod(0o600)
            log.info("Copied credentials to %s", target_creds)
    else:
        log.warning("Host credentials not found at %s — mind will need manual auth", _HOST_CREDS)

    # Log what skills are available
    skills_dir = _CONFIG_DIR / "skills"
    if skills_dir.exists():
        skill_count = len([d for d in skills_dir.iterdir() if d.is_dir()])
        log.info("Mind %s has %d skills available", MIND_ID, skill_count)
    else:
        log.warning("No skills directory found at %s", skills_dir)


# Run config setup immediately (before FastAPI starts, before harness spawns)
_setup_config_dir()


async def _fetch_secrets_on_startup():
    """Fetch all scoped secrets from the NS and inject into environment.

    Queries the NS for which secrets this mind is allowed to access,
    then fetches each one. No hardcoded secret list — the scoping
    policy in the NS determines what's available.
    """
    import aiohttp

    # Map secret key names to environment variable names
    _ENV_MAP = {
        "gh_oauth_token": "GH_TOKEN",
        "mcp_auth_token": "MCP_AUTH_TOKEN",
    }

    try:
        async with aiohttp.ClientSession() as http:
            # 1. Get the list of secrets this mind is scoped for
            async with http.get(
                f"{_NS_URL}/secrets/scopes/{MIND_NAME}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    log.debug("Could not fetch secret scopes (status=%d)", resp.status)
                    return
                scopes = await resp.json()
                secret_keys = scopes.get("secret_keys", [])

            if not secret_keys:
                log.debug("No secrets scoped for mind %s", MIND_ID)
                return

            # 2. Fetch each scoped secret
            for key in secret_keys:
                try:
                    async with http.get(
                        f"{_NS_URL}/secrets/{key}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            value = data.get("value")
                            if value:
                                env_name = _ENV_MAP.get(key, key.upper())
                                os.environ[env_name] = value
                                log.info("Secret %s loaded into %s", key, env_name)
                except Exception:
                    log.debug("Could not fetch secret %s", key)

            log.info("Loaded %d secrets for mind %s", len(secret_keys), MIND_ID)
    except Exception:
        log.debug("Could not connect to NS for secrets (NS may not be ready yet)")


@app.on_event("startup")
async def startup_secrets():
    await _fetch_secrets_on_startup()
    # Terminals outlive their sockets, so something has to collect the ones
    # nobody comes back to.
    asyncio.ensure_future(_reap_idle_ptys())


@app.get("/health")
async def health():
    return {"mind_id": MIND_ID, "status": "ok", "sessions": len(_sessions)}


@app.get("/sessions")
async def list_sessions():
    return [
        {"id": sid, "mind_id": MIND_ID, "model": s.get("model", "unknown"), "status": "running"}
        for sid, s in _sessions.items()
    ]


_PTY_CHUNK = 4096
# Winsize clamps: a hostile or confused client must not be able to set a
# degenerate or absurd terminal geometry on the pty.
_PTY_MIN_COLS, _PTY_MAX_COLS = 20, 500
_PTY_MIN_ROWS, _PTY_MAX_ROWS = 5, 200

# An unattached terminal is kept alive this long so switching browser tiles,
# locking a phone, or losing wifi does not destroy an in-flight turn.
_PTY_IDLE_TIMEOUT_S = float(os.environ.get("PTY_IDLE_TIMEOUT_SECONDS", "3600"))
_PTY_REAP_INTERVAL_S = 60.0

# Queue sentinels for the per-attachment output pump.
_PTY_EOF = object()       # the claude process exited
_PTY_EVICTED = object()   # a newer attachment took this session over


def _clamp_winsize(cols: int, rows: int) -> tuple[int, int]:
    return (
        max(_PTY_MIN_COLS, min(_PTY_MAX_COLS, cols)),
        max(_PTY_MIN_ROWS, min(_PTY_MAX_ROWS, rows)),
    )


def _set_pty_winsize(fd: int, cols: int, rows: int) -> None:
    """Set the pty's window size; the kernel delivers SIGWINCH to the client.

    tmux answers that signal by resizing the pane and repainting it from its
    own screen model, so the tile gets the current screen at its new
    geometry whether or not the app inside ever reacts to the resize.
    """
    cols, rows = _clamp_winsize(cols, rows)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


# How often to send a keepalive byte to an attached socket. A mobile
# rotation or a network blip leaves the TCP connection half-open — the
# browser keeps reporting it OPEN and doesn't fire onclose for ~30s — so the
# tile can't tell a dead socket from a merely idle one. A NUL on a fixed
# cadence (xterm ignores it) gives the browser a heartbeat to miss: when the
# gap since the last byte exceeds a few beats, the client knows to reattach
# instead of sitting black. See spark_to_bloom's socketIsStale watchdog.
_PTY_KEEPALIVE_S = 5.0
_PTY_KEEPALIVE_BYTE = b"\x00"


def _pty_control_frame(handle: "_PtyHandle", text: str) -> bool:
    """Apply a TEXT control frame to the pty; True if it was one.

    The attach protocol is: BINARY frames are raw terminal bytes, TEXT
    frames are JSON control messages (currently only resize). Non-JSON
    text is not a control frame — the caller writes it to the pty for
    backward compatibility with clients that send input as text.
    """
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(msg, dict) or msg.get("type") != "resize":
        return False
    try:
        handle.resize(int(msg["cols"]), int(msg["rows"]))
    except (KeyError, TypeError, ValueError, OSError):
        pass  # malformed or fd already closed — drop, never crash the pump
    return True


class _PtyHandle:
    """A session's `claude`, plus whichever browser tile is watching it now.

    The terminal itself lives in a tmux session named for the hive session
    (`impl.tmux_session_name`) and outlives every viewer. What this holds is
    the *client*: a `tmux attach` process in a pty of the current tile's
    geometry. Detaching (tab switch, phone lock, dropped wifi) ends the
    client and leaves `claude` running mid-turn; the next attach starts a
    fresh client, and tmux paints the whole screen into it at whatever size
    that tile happens to be. Nothing here has to remember the bytes: tmux
    keeps the screen and the scrollback, and is the one thing in this stack
    that can be *asked* what the terminal currently looks like.
    """

    __slots__ = ("session_id", "tmux_name", "claude_sid", "proc", "master_fd",
                 "cols", "rows", "queue", "detached_at", "alive", "loop")

    def __init__(self, session_id: str, tmux_name: str, claude_sid: str,
                 cols: int, rows: int):
        self.session_id = session_id
        self.tmux_name = tmux_name
        # The session's conversation id, minted by the gateway and handed
        # down. The mind records it for logging; it never chooses it.
        self.claude_sid = claude_sid
        self.cols, self.rows = _clamp_winsize(cols, rows)
        self.proc = None                 # the attached tmux client, if any
        self.master_fd: int | None = None
        self.queue: asyncio.Queue | None = None  # set while a socket is attached
        self.detached_at: float | None = time.time()
        self.alive = True
        self.loop: asyncio.AbstractEventLoop | None = None

    def resize(self, cols: int, rows: int) -> None:
        """Retarget the client's pty and let tmux redraw for the new size."""
        cols, rows = _clamp_winsize(cols, rows)
        self.cols, self.rows = cols, rows
        if self.master_fd is not None:
            _set_pty_winsize(self.master_fd, cols, rows)

    def push(self, data: bytes) -> None:
        """Send bytes to the attached socket, if one is attached."""
        if data and self.queue is not None:
            self.queue.put_nowait(data)

    def signal(self, sentinel: object) -> None:
        if self.queue is not None:
            self.queue.put_nowait(sentinel)


_ptys: dict[str, _PtyHandle] = {}


def _register_pty_reader(handle: _PtyHandle) -> None:
    """Forward everything the attached client emits, for as long as it lives.

    Re-registered on every attach: the fd belongs to the client, and each
    attach brings a new one. EOF here means the client ended — either
    because we detached it or because `claude` exited and took the tmux
    session with it — so the socket is told and the liveness question is
    left to tmux, which is the only thing that knows.
    """
    _unregister_pty_reader(handle)
    if handle.master_fd is None:
        return
    loop = asyncio.get_event_loop()
    handle.loop = loop
    fd = handle.master_fd

    def _on_readable() -> None:
        try:
            data = os.read(fd, _PTY_CHUNK)
        except OSError:
            data = b""
        if not data:
            try:
                loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            handle.signal(_PTY_EOF)
            return
        handle.push(data)

    loop.add_reader(fd, _on_readable)


def _unregister_pty_reader(handle: _PtyHandle) -> None:
    if handle.loop is not None and handle.master_fd is not None and not handle.loop.is_closed():
        try:
            handle.loop.remove_reader(handle.master_fd)
        except (ValueError, OSError, RuntimeError):
            pass


def _detach_client(handle: _PtyHandle) -> None:
    """End the viewing client, leaving the conversation running in tmux."""
    _unregister_pty_reader(handle)
    if handle.master_fd is not None:
        try:
            os.close(handle.master_fd)
        except OSError:
            pass
        handle.master_fd = None
    proc, handle.proc = handle.proc, None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _teardown_pty(session_id: str) -> bool:
    """End a session's terminal for good. True if one existed.

    Kills the tmux session, which is what actually owns `claude` — detaching
    the client alone would leave the conversation running with nobody able
    to reach it.
    """
    handle = _ptys.pop(session_id, None)
    if handle is None:
        return False
    handle.alive = False
    handle.signal(_PTY_EOF)
    _detach_client(handle)
    impl.kill_pty_session(session_id)
    log.info("Tore down terminal for session %s (tmux=%s)", session_id, handle.tmux_name)
    return True


async def _reap_idle_ptys() -> None:
    """Kill terminals whose `claude` exited, or that nobody came back to."""
    while True:
        await asyncio.sleep(_PTY_REAP_INTERVAL_S)
        now = time.time()
        for session_id, handle in list(_ptys.items()):
            if not impl.pty_session_alive(session_id):
                _teardown_pty(session_id)
            elif (handle.queue is None and handle.detached_at is not None
                    and now - handle.detached_at > _PTY_IDLE_TIMEOUT_S):
                log.info("Reaping terminal for session %s — unattached for %.0fs",
                         session_id, now - handle.detached_at)
                _teardown_pty(session_id)


def _open_pty_session(
    session_id: str, model: str, resume_sid: str | None, cols: int, rows: int,
    client_ref: str | None = None, owner_type: str | None = None, owner_ref: str | None = None,
) -> _PtyHandle:
    """Attach this tile to the session's terminal, starting one if needed.

    The `claude` process is keyed by session and reused; only the viewing
    client is per-attach. A tile arriving while another holds the session
    evicts it first — one conversation, one keyboard — and the eviction is
    announced before the old client dies so the displaced tile is told it
    was replaced rather than that the terminal exited.
    """
    handle = _ptys.get(session_id)
    if handle is not None:
        handle.signal(_PTY_EVICTED)
        _detach_client(handle)
    else:
        handle = _PtyHandle(session_id, impl.tmux_session_name(session_id),
                            resume_sid or "", cols, rows)
        _ptys[session_id] = handle

    registry, config_obj, mcp_config = _load_registry_and_mcp_config()
    proc, master_fd = impl.spawn_pty(
        session_id=session_id,
        model=model,
        resume_sid=resume_sid,
        mcp_config=mcp_config,
        registry=registry,
        config_obj=config_obj,
        mind_id=MIND_ID,
        mind_name=MIND_NAME,
        cols=cols,
        rows=rows,
        client_ref=client_ref,
        owner_type=owner_type,
        owner_ref=owner_ref,
    )
    handle.proc = proc
    handle.master_fd = master_fd
    handle.cols, handle.rows = _clamp_winsize(cols, rows)
    handle.queue = None
    handle.alive = True
    _register_pty_reader(handle)
    return handle


@app.websocket("/sessions/{session_id}/attach-pty")
async def attach_pty(
    websocket: WebSocket,
    session_id: str,
    resume_sid: str | None = None,
    model: str = "sonnet",
    cols: int = 80,
    rows: int = 24,
    client_ref: str | None = None,
    owner_type: str | None = None,
    owner_ref: str | None = None,
):
    """Bidirectional raw-byte bridge between a browser terminal and this
    session's interactive `claude`.

    The terminal is keyed by ``session_id`` and outlives the socket: it runs
    in tmux, so attaching starts a client on the *existing* conversation and
    tmux paints the current screen into it, rather than spawning a second
    `claude` on the same transcript. Only an explicit kill (or the idle
    reaper) ends it — so switching tiles mid-turn no longer throws the turn
    away, and two open tiles can never end up driving each other's
    conversation.

    Independent of the non-interactive stream-json process tracked in
    `_sessions` — the caller (hive-comms) supplies `resume_sid`/`model` as
    query params so this route never needs its own session lookup. Both
    processes append to the same conversation file; only one writes at a
    time.

    ``cols``/``rows`` size the pty the client runs in; later TEXT frames
    carrying ``{"type":"resize","cols":N,"rows":M}`` retarget it live, and
    tmux redraws the pane for the new geometry.
    """
    await websocket.accept()

    if not hasattr(impl, "spawn_pty"):
        await websocket.close(code=1011, reason=f"attach-pty not supported by mind {MIND_NAME}")
        return

    # Attaching means attaching. Without the session's conversation id there
    # is nothing to attach TO, and opening a terminal anyway is how a live
    # session came up blank — so refuse and let the caller show the failure.
    if not (resume_sid or "").strip():
        log.warning("attach-pty for session %s carried no conversation id", session_id)
        await websocket.close(code=1008, reason="no conversation id for this session")
        return

    # hive-comms has no chat-id-like concept for a browser tile — Telegram/
    # Discord send a real client_ref, a terminal attach never will. Without
    # one, rotation_check's missing-client_ref guard bails on every fire and
    # the transcript just grows until Claude's own native compaction steps
    # in. session_id (the gateway's conversation id) is already unique and
    # stable for this conversation's lifetime, so it doubles as the key.
    client_ref = client_ref or session_id

    cols, rows = _clamp_winsize(cols, rows)
    try:
        handle = _open_pty_session(
            session_id, model, resume_sid, cols, rows,
            client_ref=client_ref, owner_type=owner_type, owner_ref=owner_ref,
        )
    except Exception:
        log.exception("Failed to open terminal for session %s", session_id)
        await websocket.close(code=1011, reason="failed to start terminal")
        return

    queue: asyncio.Queue = asyncio.Queue()
    handle.queue = queue
    handle.detached_at = None

    async def pump_to_ws() -> None:
        # No backlog to replay: the client was spawned at this tile's
        # geometry and tmux draws the whole screen into it on attach.
        while True:
            item = await queue.get()
            if item is _PTY_EOF:
                await websocket.close(code=1000, reason="terminal exited")
                return
            if item is _PTY_EVICTED:
                await websocket.close(code=1012, reason="attached elsewhere")
                return
            await websocket.send_bytes(item)

    async def keepalive() -> None:
        # A heartbeat the browser can miss: the NUL rides the same queue as
        # real output (so the pump stays the only sender) and xterm ignores
        # it. Its absence is how a half-open socket is detected client-side
        # long before the browser's own ~30s dead-connection timeout would
        # fire onclose.
        while True:
            await asyncio.sleep(_PTY_KEEPALIVE_S)
            queue.put_nowait(_PTY_KEEPALIVE_BYTE)

    pump_out_task = asyncio.ensure_future(pump_to_ws())
    keepalive_task = asyncio.ensure_future(keepalive())

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is None and msg.get("text") is not None:
                if _pty_control_frame(handle, msg["text"]):
                    continue
                data = msg["text"].encode()
            if data and handle.master_fd is not None:
                os.write(handle.master_fd, data)
    except (WebSocketDisconnect, OSError):
        pass
    finally:
        pump_out_task.cancel()
        keepalive_task.cancel()
        # Detach only — `claude` keeps running inside tmux, so the turn in
        # flight survives and the next attach sees how it finished. Clear
        # the queue first so ending the client doesn't look like an exit.
        if handle.queue is queue:
            handle.queue = None
            handle.detached_at = time.time()
            _detach_client(handle)
        log.info("Detached from terminal for session %s (tmux=%s, still running=%s)",
                 session_id, handle.tmux_name, impl.pty_session_alive(session_id))


def _extract_assistant_texts(event: dict) -> list[str]:
    """Return the text of every ``text`` content block in an assistant event.

    Deterministically ignores non-assistant events (system, result,
    stream_event deltas, etc.) and non-text blocks (tool_use, tool_result).
    """
    if event.get("type") != "assistant":
        return []
    message = event.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if text:
                texts.append(text)
    return texts


def _mirror_turn_to_pty(session_id: str | None, user_text: str | None, assistant_texts: list[str]) -> None:
    """Render a stream-json-surface turn into this session's live terminal, if any.

    The interactive terminal only shows what its own `claude` process drew.
    A turn delivered through the non-interactive stream-json path (Telegram
    today; any future non-terminal surface) never touches that process, so
    without this the terminal silently drifts out of sync with what was
    actually said and heard.

    Written straight to the attached socket rather than into the pane: tmux
    owns the pane's contents and has no way to be told about bytes it didn't
    produce, so this is a live overlay for whoever is watching now, cleared
    by the next repaint the way it always was by the next paint. A no-op
    when nobody is attached, which is the common case — most Telegram
    sessions never get a terminal.
    """
    if not assistant_texts or not session_id:
        return
    handle = _ptys.get(session_id)
    if handle is None:
        return
    owner_label = os.environ.get("OWNER_NAME", "user")
    parts = ["\r\n"]
    if user_text:
        parts.append(
            f"\x1b[36m[telegram] {owner_label}:\x1b[0m "
            + user_text.replace("\n", "\r\n") + "\r\n"
        )
    reply = "\n\n".join(assistant_texts).replace("\n", "\r\n")
    parts.append(f"\x1b[32m[telegram] {MIND_NAME}:\x1b[0m {reply}\r\n")
    handle.push("".join(parts).encode())


def _proactive_chat_id(session: dict) -> int | None:
    """Telegram chat id for this session, or None if the surface can't take one.

    ``chat_id`` holds the surface's ``client_ref``. Telegram's is a numeric
    chat id; the web terminal's is a ``terminal-<uuid>`` tile ref, which has
    nowhere to deliver an unsolicited turn.
    """
    chat_id = session.get("chat_id")
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None


async def _flush_unsolicited(session: dict, proc: object, session_id: str) -> int:
    """Consume harness output produced before this turn was requested.

    Called with the stdout lock held, immediately before writing a user
    message. Any line already waiting belongs to an unsolicited turn (a
    background task notification, a proactive turn). Assistant text is routed
    to the proactive queue when the surface can take it; everything else is
    dropped. Returns the number of lines consumed, for logging and tests.
    """
    from bots import proactive

    stdout = getattr(proc, "stdout", None)
    if stdout is None:
        return 0

    chat_id = _proactive_chat_id(session)
    consumed = 0
    while True:
        try:
            line = await asyncio.wait_for(stdout.readline(), timeout=0.2)
        except asyncio.TimeoutError:
            break
        if not line:  # EOF — subprocess exited
            break
        consumed += 1
        decoded = line.decode().strip()
        if not decoded or chat_id is None:
            continue
        try:
            event = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        texts = _extract_assistant_texts(event)
        for text in texts:
            proactive.enqueue(chat_id, text)
        if texts:
            _mirror_turn_to_pty(session_id, None, texts)

    if consumed:
        log.warning(
            "Flushed %d unsolicited line(s) before send on session %s",
            consumed, session_id,
        )
    return consumed


async def _idle_drain(session: dict) -> None:
    """Push unsolicited assistant output to Telegram while no request is live.

    Runs per CLI session for its lifetime. When a request is in flight the
    solicited reader in ``send_message`` owns stdout, so this backs off. When
    idle it acquires the stdout lock, reads harness stream-json lines, and
    enqueues assistant ``text`` blocks for proactive delivery. Cancelled by
    ``kill_session``.
    """
    from bots import proactive

    proc = session.get("proc")
    if proc is None or getattr(proc, "stdout", None) is None:
        return

    lock: asyncio.Lock = session["stdout_lock"]
    while True:
        if session.get("in_flight"):
            await asyncio.sleep(0.2)
            continue

        async with lock:
            # Re-check after acquiring: a request may have started while we
            # were waiting for the lock. If so, back off and let it read.
            if session.get("in_flight"):
                await asyncio.sleep(0.2)
                continue
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if not line:  # EOF — subprocess exited
                return
            decoded = line.decode().strip()
            if not decoded:
                continue
            try:
                event = json.loads(decoded)
            except json.JSONDecodeError:
                continue
            texts = _extract_assistant_texts(event)
            if texts:
                _mirror_turn_to_pty(session.get("session_id"), None, texts)
            chat_id = _proactive_chat_id(session)
            if chat_id is None:
                continue
            for text in texts:
                proactive.enqueue(chat_id, text)


async def _drain_stderr(session: dict, session_id: str) -> None:
    """Log subprocess stderr and keep a tail for post-mortem error reporting.

    stderr is a pipe; without a reader the harness's death reason is lost
    (and a chatty subprocess would block on a full pipe buffer). Runs per
    CLI session for its lifetime; exits on EOF when the subprocess dies.
    """
    from collections import deque

    proc = session.get("proc")
    if proc is None or getattr(proc, "stderr", None) is None:
        return

    tail: deque[str] = deque(maxlen=40)
    session["stderr_tail"] = tail
    while True:
        line = await proc.stderr.readline()
        if not line:  # EOF — subprocess exited
            rc = getattr(proc, "returncode", None)
            if rc not in (None, 0):
                log.error(
                    "Session %s subprocess (pid=%s) exited rc=%s; stderr tail:\n%s",
                    session_id, getattr(proc, "pid", "?"), rc, "\n".join(tail),
                )
            return
        decoded = line.decode(errors="replace").rstrip()
        if decoded:
            tail.append(decoded)
            log.warning("Session %s stderr: %s", session_id, decoded)


def _dead_proc_response(session_id: str, session: dict, proc) -> JSONResponse:
    """Clean up a session whose subprocess died and report why.

    Returns 404 (not 500) so the NS gateway's respawn-on-404 path recreates
    the session instead of surfacing an opaque failure to the user.
    """
    rc = getattr(proc, "returncode", None) if proc else None
    stderr_tail = "\n".join(session.get("stderr_tail") or []) or "<no stderr captured>"
    log.error(
        "Session %s: harness subprocess is dead (returncode=%s) — removing session. "
        "stderr tail:\n%s",
        session_id, rc, stderr_tail,
    )
    _sessions.pop(session_id, None)
    for task_key in ("drain_task", "stderr_task"):
        task = session.get(task_key)
        if task is not None and not task.done():
            task.cancel()
    return JSONResponse(
        {
            "error": (
                f"Session {session_id} harness subprocess exited "
                f"(returncode={rc}). stderr tail: {stderr_tail[-2000:]}"
            )
        },
        status_code=404,
    )


def _load_registry_and_mcp_config() -> tuple[object | None, object | None, str]:
    """Build the model registry and resolve the MCP config path, if configured.

    Shared by every route that spawns a harness process (session create, pty
    attach) — kept in one place so the two spawn paths can't drift apart.
    """
    registry = None
    config_obj = None
    mcp_config = ""
    try:
        from config import config as _config
        config_obj = _config
        from models import ModelRegistry, Provider
        providers = {}
        for pname, pdata in _config.providers.items():
            env_overrides = pdata.get("env", {}) if isinstance(pdata, dict) else {}
            providers[pname] = Provider(name=pname, env_overrides=env_overrides)
        registry = ModelRegistry(providers=providers, static_models=_config.models)
        mcp_config_path = PROJECT_DIR / ".mcp.container.json"
        if not mcp_config_path.exists():
            mcp_config_path = PROJECT_DIR / ".mcp.json"
        mcp_config = str(mcp_config_path) if mcp_config_path.exists() else ""
    except ImportError:
        pass
    return registry, config_obj, mcp_config


@app.post("/sessions")
async def create_session(request: Request):
    body = await request.json()
    # Ids are the gateway's to mint — both of them. A mind that invents a
    # session id or a conversation id creates a session nothing else knows
    # about, which is exactly how a terminal ends up talking to itself.
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse({"error": "session_id required"}, status_code=400)
    model = body.get("model", "sonnet")
    resume_sid = (body.get("resume_sid") or "").strip()
    if not resume_sid:
        return JSONResponse(
            {"error": "resume_sid (conversation id) required — the gateway mints it"},
            status_code=400,
        )
    surface_prompt = body.get("surface_prompt")
    autopilot = body.get("autopilot", False)
    allowed_directories = body.get("allowed_directories")
    client_ref = body.get("client_ref")
    owner_type = body.get("owner_type")
    owner_ref = body.get("owner_ref")
    surface = body.get("surface")
    # NS composes the system-prompt content (soul + standing + recent +
    # session-memory) per-mind in comms/bootstrap_loader.py and ships it
    # here. The mind passes it straight to `claude --append-system-prompt`.
    system_prompt_blocks = body.get("system_prompt_blocks", "")

    try:
        registry, config_obj, mcp_config = _load_registry_and_mcp_config()

        proc = await impl.spawn(
            session_id=session_id,
            model=model,
            autopilot=autopilot,
            resume_sid=resume_sid,
            surface_prompt=surface_prompt,
            allowed_directories=allowed_directories,
            mind_id=MIND_ID,
            mind_name=MIND_NAME,
            system_prompt_blocks=system_prompt_blocks,
            mcp_config=mcp_config,
            registry=registry,
            config_obj=config_obj,
            client_ref=client_ref,
            owner_type=owner_type,
            owner_ref=owner_ref,
            surface=surface,
        )

        session = {
            "proc": proc,
            "session_id": session_id,
            "model": model,
            "resume_sid": resume_sid,
            "cancel_event": asyncio.Event(),
            # client_ref is the surface's chat id (Telegram chat_id). Persist it
            # so unsolicited (proactive) turns can be routed back to the user.
            "chat_id": client_ref,
            # Serialises stdout access between the live request reader
            # (send_message) and the background idle drain.
            "stdout_lock": asyncio.Lock(),
            "in_flight": False,
        }
        _sessions[session_id] = session

        # CLI-based minds expose a real subprocess with .stdout we can drain.
        # SDK/Codex minds route output through impl.send instead — skip them.
        if not hasattr(impl, "send") and getattr(proc, "stdout", None) is not None:
            session["drain_task"] = asyncio.ensure_future(_idle_drain(session))
        if getattr(proc, "stderr", None) is not None:
            session["stderr_task"] = asyncio.ensure_future(_drain_stderr(session, session_id))

        log.info("Session %s created for %s (model=%s)", session_id, MIND_ID, model)
        return {"session_id": session_id, "mind_id": MIND_ID, "status": "running", "model": model}

    except Exception as exc:
        log.exception("Failed to create session for %s", MIND_ID)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/sessions/{session_id}/message")
async def send_message(session_id: str, request: Request):
    body = await request.json()
    content = body.get("content", "")
    images: list[dict] | None = body.get("images")

    session = _sessions.get(session_id)
    if not session:
        return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)

    # Turn-bleed guard. If a previous turn's stream was abandoned mid-response
    # (client disconnect, voice timeout, etc.), the underlying claude/codex
    # subprocess kept generating, and its output is still buffered in proc.stdout.
    # A second message arriving now would write to stdin and immediately read
    # the *previous* turn's queued result. Refuse to start a second turn while
    # one is in flight; caller retries.
    if session.get("in_flight"):
        return JSONResponse(
            {"error": "Turn in progress, retry shortly"},
            status_code=409,
        )

    proc = session["proc"]

    # Check if implementation has a custom send function (SDK/Codex minds)
    if hasattr(impl, "send"):
        # SDK implementations (Bilby) require the async generator to be consumed
        # in the same task that created it (anyio cancel scope constraint).
        # Collect all events first, then stream them out.
        session["in_flight"] = True
        try:
            collected_events = []
            async for event in impl.send(session_id, content, db=None):
                collected_events.append(event)
        finally:
            session["in_flight"] = False

        async def stream_collected():
            for event in collected_events:
                yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(stream_collected(), media_type="text/event-stream")

    # CLI-based minds: write to stdin, read from stdout
    if not proc or not proc.stdin or proc.returncode is not None:
        return _dead_proc_response(session_id, session, proc)

    # Send the message as a stream-json input
    content_blocks: list[dict] = []
    if images:
        for img in images:
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            })
    content_blocks.append({"type": "text", "text": content})
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
    })
    session["in_flight"] = True

    async def stream_response():
        cancel_event = session.get("cancel_event")
        stdout_lock = session.get("stdout_lock")
        assistant_texts: list[str] = []
        try:
            # Hold the stdout lock for the whole read so the idle drain can
            # never consume this turn's output concurrently.
            if stdout_lock is not None:
                await stdout_lock.acquire()
            # Anything already in the pipe belongs to a turn nobody asked for
            # — a completed background task notification is a full turn with
            # its own `result` event. Left there it would satisfy and close
            # this request, and our real answer would be handed to whatever
            # the user sends next: a permanent one-turn lag. Flush it first.
            await _flush_unsolicited(session, proc, session_id)
            proc.stdin.write(msg.encode() + b"\n")
            await proc.stdin.drain()
            while not (cancel_event and cancel_event.is_set()):
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not line:  # EOF — subprocess exited
                    break
                decoded = line.decode().strip()
                if not decoded:
                    continue
                yield f"data: {decoded}\n\n"
                try:
                    event = json.loads(decoded)
                    assistant_texts.extend(_extract_assistant_texts(event))
                    if event.get("type") == "result":
                        break
                except json.JSONDecodeError:
                    continue
        finally:
            # Mirror the completed turn into this session's terminal, if one
            # is open — a Telegram exchange must not go invisible to a
            # browser tile watching the same conversation. Skipped on an
            # aborted turn (cancel/disconnect before any text arrived).
            if assistant_texts:
                _mirror_turn_to_pty(session_id, content, assistant_texts)
            if stdout_lock is not None and stdout_lock.locked():
                stdout_lock.release()
            # Clear in_flight on every exit path: normal completion, generator
            # cancellation (client disconnect), interrupt, or exception.
            session["in_flight"] = False

    return StreamingResponse(stream_response(), media_type="text/event-stream")


@app.post("/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)

    if not session.get("in_flight"):
        return {"ok": True, "session_id": session_id, "message": "nothing_running"}

    # 1. Signal the streaming generator to exit on its next timeout tick
    cancel_event = session.get("cancel_event")
    if cancel_event:
        cancel_event.set()

    # 2. Send SIGINT to the Claude subprocess so it stops generating
    proc = session.get("proc")
    if proc and (not hasattr(proc, "returncode") or proc.returncode is None):
        try:
            proc.send_signal(signal.SIGINT)
            log.info("Sent SIGINT to session %s", session_id)
        except ProcessLookupError:
            pass

    # 3. Wait for the generator's finally block to clear in_flight (up to 3s)
    for _ in range(30):
        if not session.get("in_flight"):
            break
        await asyncio.sleep(0.1)
    session["in_flight"] = False  # force-clear if still stuck

    # 4. Drain any buffered subprocess output to prevent turn-bleed
    if proc and (not hasattr(proc, "returncode") or proc.returncode is None):
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.2)
                    if not line:
                        break
                except asyncio.TimeoutError:
                    break
        except Exception:
            pass

    # 5. Reset the cancel event so the next turn starts clean
    if cancel_event:
        cancel_event.clear()

    log.info("Interrupt complete for session %s — ready for next turn", session_id)
    return {"ok": True, "session_id": session_id, "ready": True}


async def _teardown_stream_process(session_id: str) -> bool:
    """End the non-interactive (stream-json) harness process. True if one ran.

    The conversation is untouched: it lives in the transcript, and the next
    spawn resumes it. This is what a surface handover ends — the *process*,
    not the thread of conversation.
    """
    session = _sessions.pop(session_id, None)
    if not session:
        return False

    # Cancel per-session reader tasks so no orphan keeps a pipe open.
    for task_key in ("drain_task", "stderr_task"):
        task = session.get(task_key)
        if task is not None and not task.done():
            task.cancel()

    proc = session.get("proc")
    proc_pid = getattr(proc, "pid", None)
    log.info("KILL_DIAG: popped session %s — proc.pid=%s proc.returncode=%s impl.has_kill=%s",
             session_id, proc_pid, getattr(proc, "returncode", "n/a"), hasattr(impl, "kill"))
    try:
        if hasattr(impl, "kill"):
            # SDK/Codex minds have a custom kill that takes session_id
            if "session_id" in impl.kill.__code__.co_varnames:
                log.info("KILL_DIAG: calling impl.kill(session_id=%s)", session_id)
                await impl.kill(session_id)
            else:
                log.info("KILL_DIAG: calling impl.kill(proc) for pid=%s", proc_pid)
                await impl.kill(proc)
        elif proc and proc.returncode is None:
            log.info("KILL_DIAG: fallback proc.terminate() for pid=%s", proc_pid)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("KILL_DIAG: SIGTERM timed out, sending SIGKILL to pid=%s", proc_pid)
                proc.kill()
                await proc.wait()
    except Exception:
        log.exception("KILL_DIAG: Error killing session %s", session_id)

    log.info("KILL_DIAG: kill complete for session=%s pid=%s post_returncode=%s",
             session_id, proc_pid, getattr(proc, "returncode", "n/a") if proc else "no-proc")
    return True


@app.post("/sessions/{session_id}/release")
async def release_session_surface(session_id: str, surface: str):
    """End one surface's process for a session, leaving the conversation open.

    A conversation may only ever have one live harness process — two would
    both hold it in memory, neither would see the other's turns, and each
    would append turns the other doesn't know happened. So handing a session
    from the browser terminal to Telegram (or back) means ending the
    outgoing process first, which is what this does. The transcript is the
    handover medium: the incoming surface spawns with `--resume` and reads
    everything the outgoing one said.

    `surface=terminal` ends the tmux session; `surface=stream` ends the
    stream-json process. Idempotent — releasing a surface that isn't running
    reports `released: false` rather than failing.
    """
    if surface == "terminal":
        released = _teardown_pty(session_id)
    elif surface == "stream":
        released = await _teardown_stream_process(session_id)
    else:
        return JSONResponse(
            {"error": f"Unknown surface: {surface!r} (expected 'terminal' or 'stream')"},
            status_code=400,
        )
    log.info("Released %s surface for session %s (was running=%s)", surface, session_id, released)
    return {"session_id": session_id, "surface": surface, "released": released}


@app.delete("/sessions/{session_id}")
async def kill_session(session_id: str):
    log.info("KILL_DIAG: DELETE called for session_id=%s; _sessions has keys=%s",
             session_id, list(_sessions.keys()))
    # The terminal is keyed by session, not by socket, so ending the session
    # is the only thing that ends it. Do this before the _sessions lookup —
    # a terminal-born session never had a stream-json process to track.
    had_pty = _teardown_pty(session_id)
    had_stream = await _teardown_stream_process(session_id)

    if not had_stream:
        if had_pty:
            log.info("KILL_DIAG: session %s was terminal-only — pty killed, no stream-json proc", session_id)
            return {"session_id": session_id, "status": "closed"}
        log.warning("KILL_DIAG: session %s NOT in _sessions — returning 404, NO subprocess kill attempted", session_id)
        return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)

    return {"session_id": session_id, "status": "closed"}



if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
