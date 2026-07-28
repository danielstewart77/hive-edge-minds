"""Claude CLI + Claude models template.

Tested. Based on Ada's implementation.
Spawns a `claude` CLI subprocess in stream-json mode.
"""

import asyncio
import logging
import fcntl
import os
import pty
import shlex
import signal
import struct
import subprocess
import termios
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _ctx1m(model: str) -> str:
    """Pin the 1M-context variant of a Claude model.

    The gateway stores bare aliases (``opus``, ``sonnet``); against a custom
    ``ANTHROPIC_BASE_URL`` the harness does not auto-upgrade those to the 1M
    window, so a bare alias runs a 200k conversation that the rotation
    threshold (sized for 1M-era sessions) may never catch before the API
    rejects with "Prompt is too long". Appending ``[1m]`` requests the
    long-context variant explicitly. Models that already carry a variant
    suffix, haiku (no 1M variant), and hosts that opted out via
    ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` pass through untouched.
    """
    if os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT"):
        return model
    if not model or "[" in model or "haiku" in model:
        return model
    return f"{model}[1m]"


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
) -> asyncio.subprocess.Process:
    _log = logger or log

    # NS composed system_prompt_blocks (soul + standing + recent +
    # session-memory). The surface prompt (per-bot, e.g. Telegram surface)
    # appends, separated by a blank line.
    if system_prompt_blocks and surface_prompt:
        full_prompt = f"{system_prompt_blocks}\n\n{surface_prompt}"
    elif surface_prompt:
        full_prompt = surface_prompt
    else:
        full_prompt = system_prompt_blocks

    cmd = [
        "claude", "-p",
        "--verbose",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--forward-subagent-text",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--model", _ctx1m(model),
        "--mcp-config", mcp_config,
        "--append-system-prompt", full_prompt,
    ]
    if autopilot:
        if config_obj:
            cmd.extend(["--max-budget-usd", str(config_obj.autopilot_guards.max_budget_usd)])
    for d in allowed_directories or []:
        cmd.extend(["--allowedDirectory", d])

    from config import PROJECT_DIR

    if not resume_sid:
        raise ValueError(
            f"spawn for session {session_id} got no conversation id — "
            "the gateway mints one at session creation and must pass it"
        )
    cmd.extend(_conversation_flags(resume_sid, PROJECT_DIR))

    provider = registry.get_provider(model) if registry else None
    env = os.environ.copy()
    if provider:
        env.update(provider.env_overrides)
    if is_group_session:
        env["HIVEMIND_GROUP_SESSION"] = "1"
    # Per-session env for hooks (rotation_check, etc.) inside the subprocess.
    if client_ref:
        env["CLIENT_REF"] = client_ref
    owner_type = kwargs.get("owner_type") or ""
    owner_ref = kwargs.get("owner_ref") or ""
    if owner_type:
        env["OWNER_TYPE"] = owner_type
    if owner_ref:
        env["OWNER_REF"] = owner_ref
    # The surface this process serves (telegram, discord, ...), stamped on
    # the env so the surface_inject.sh UserPromptSubmit hook can tell the
    # model where each turn arrived. The gateway sends the clean label; the
    # owner_type prefix is only a fallback for a gateway that predates it.
    surface = kwargs.get("surface") or owner_type.split(":", 1)[0]
    if surface:
        env["HIVE_SURFACE"] = surface

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
        env=env,
        cwd=str(PROJECT_DIR),
    )
    _log.info("Spawned CLI process for session %s (pid=%d, model=%s)", session_id, proc.pid, model)
    return proc


def _claude_transcript_exists(claude_sid: str, project_dir: Path) -> bool:
    """True if the claude CLI has a transcript for this session id.

    Claude Code stores conversations at
    ``<config>/projects/<slugified-cwd>/<sid>.jsonl`` where the slug is the
    cwd path with every ``/``, ``_`` and ``.`` turned into ``-``.
    """
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    slug = str(project_dir).replace("/", "-").replace("_", "-").replace(".", "-")
    return (config_dir / "projects" / slug / f"{claude_sid}.jsonl").exists()


def _conversation_flags(claude_sid: str, project_dir: Path) -> list[str]:
    """CLI flags that bind a harness process to one conversation id.

    A transcript on disk means the conversation has spoken before, so resume
    it in place. No transcript means this is its first process, so declare
    the id. Either way the id is the caller's, never the harness's — the two
    branches are the same conversation at different ages, not "existing" and
    "new".
    """
    if _claude_transcript_exists(claude_sid, project_dir):
        return ["--resume", claude_sid]
    return ["--session-id", claude_sid]


# The web terminal's `claude` runs inside tmux rather than on a bare pty.
# A bare pty gives a byte stream and a winsize and nothing else: no way to
# read what is on screen and no way to ask for a redraw, so every attach and
# every resize had to be reconstructed by hand. tmux already keeps a screen
# model per pane and repaints it for whatever geometry a client attaches at
# — measured against an app that ignores SIGWINCH entirely, which is the
# worst case and the one we have. So the session owns a tmux session, and a
# browser attach is just a tmux client living in a pty we hold.
#
# A dedicated socket: this server is ours, so its options and its lifetime
# can't be disturbed by a tmux the operator runs by hand.
TMUX_SOCKET = "MIND_NAME-terminal"

# Applied ahead of every session creation, so they hold from the pane's first
# byte. `history-limit` and `default-terminal` are read when the pane is
# created and can't be retrofitted; `exit-empty off` keeps the server up
# between the last session ending and the next one starting; `prefix None`
# and `escape-time 0` keep tmux's own key handling out of a TUI that wants
# every keystroke, Escape most of all; `status off` gives the pane the row
# the status bar would take; `window-size latest` sizes the window to the
# client that most recently attached, which is the only client we allow.
_TMUX_OPTIONS = [
    ["set", "-g", "exit-empty", "off"],
    ["set", "-g", "status", "off"],
    ["set", "-g", "prefix", "None"],
    ["set", "-g", "escape-time", "0"],
    ["set", "-g", "history-limit", "50000"],
    ["set", "-g", "window-size", "latest"],
    ["set", "-g", "destroy-unattached", "off"],
    ["set", "-g", "default-terminal", "tmux-256color"],
    ["set", "-gas", "terminal-features", ",xterm-256color:RGB"],
]

# What the browser is: xterm.js, which reads xterm-256color and renders
# 24-bit colour. The pane's own TERM is tmux-256color, set above.
_CLIENT_TERM = "xterm-256color"


def tmux_session_name(session_id: str) -> str:
    """The tmux session that holds this hive session's terminal."""
    return f"MIND_NAME-{session_id}"


def _take_controlling_tty() -> None:
    """Make the pty the child's controlling terminal, in the child.

    `setsid` alone leaves the process with no controlling terminal at all,
    and the kernel sends SIGWINCH to a terminal's foreground process group —
    of which there is none. That is why a resize used to reach the pty and
    stop there: the winsize changed, nothing was ever signalled, and the
    app on the far end went on rendering for the size it started at. The
    session leader has to claim the tty for the signal to have somewhere to
    go. stdin is the pty slave by the time this runs.
    """
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _terminal_argv(model: str, mcp_config: str, resume_sid: str, project_dir: Path) -> list[str]:
    """The interactive `claude` that runs inside the tmux pane."""
    cmd = [
        "claude",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--model", _ctx1m(model),
    ]
    if mcp_config:
        cmd.extend(["--mcp-config", mcp_config])
    cmd.extend(_conversation_flags(resume_sid, project_dir))
    return cmd


def _rotation_argv(model: str, mcp_config: str, new_claude_sid: str) -> list[str]:
    """The interactive `claude` a rotation respawns the pane onto.

    Always `--session-id`, never `--resume`: rotation starts a fresh harness
    conversation under the same hive session, and it opens holding a summary
    of the one it replaced. The summary itself is not here — see
    `_seeded_pane_command`.
    """
    cmd = [
        "claude",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--model", _ctx1m(model),
    ]
    if mcp_config:
        cmd.extend(["--mcp-config", mcp_config])
    cmd.extend(["--session-id", new_claude_sid])
    return cmd


# A single argv entry cannot exceed MAX_ARG_STRLEN (32 pages, 128 KiB on
# Linux); exec fails outright above it. The seed reaches the harness as one
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
    shell that then `exec`s the harness: the tmux command stays short
    regardless of how much context the successor is carrying.
    """
    if not system_prompt:
        return argv

    system_prompt = _capped_seed(system_prompt)
    seed_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/tmp")) / "rotation-seeds"
    seed_dir.mkdir(parents=True, exist_ok=True)
    seed_file = seed_dir / f"{seed_key}.txt"
    seed_file.write_text(system_prompt)
    quoted_seed = shlex.quote(str(seed_file))
    harness = " ".join(shlex.quote(arg) for arg in argv)
    # Read then delete: the seed is one process's opening context, and it is
    # the whole conversation's memory sitting in a world-readable file.
    return [
        "/bin/sh", "-c",
        f'seed=$(cat {quoted_seed}); rm -f {quoted_seed}; '
        f'exec {harness} --append-system-prompt "$seed"',
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
    if _tmux("kill-session", "-t", f"={name}").returncode != 0:
        return False
    log.info("Killed tmux session %s", name)
    return True


def rotate_pty_session(
    session_id: str,
    new_claude_sid: str,
    model: str,
    system_prompt: str = "",
    mcp_config: str = "",
    registry: Any = None,
    client_ref: str | None = None,
    owner_type: str | None = None,
    owner_ref: str | None = None,
    **kwargs: Any,
) -> bool:
    """Start a fresh harness conversation in a live terminal, in place.

    A rotation replaces the *conversation*, not the session and not the
    terminal. The hive session id is permanent — it is what every surface,
    label and ledger row is keyed to — so nothing here renames anything. The
    pane's process is respawned onto a new harness conversation seeded with
    the carry-forward, and the attached tmux client (and therefore the pty,
    the websocket and the browser tile above it) is never disturbed. The
    user keeps typing into the same pane under the same id; only the context
    behind it turned over.

    ``system_prompt`` is the rotation carry-forward, delivered by
    ``_seeded_pane_command``. Returns False when there was no live terminal
    to rotate.
    """
    del kwargs  # unused, kept for call-site symmetry with spawn_pty()

    from config import PROJECT_DIR

    name = tmux_session_name(session_id)

    if not pty_session_alive(session_id):
        log.info("No live terminal for session %s — nothing to rotate in place",
                 session_id)
        return False

    cmd = _seeded_pane_command(
        _rotation_argv(model, mcp_config, new_claude_sid), system_prompt, new_claude_sid
    )

    env = os.environ.copy()
    overrides: dict[str, str] = {}
    provider = registry.get_provider(model) if registry else None
    if provider:
        overrides.update(provider.env_overrides)
    overrides["CLAUDE_CODE_DISABLE_AGENT_VIEW"] = "1"
    overrides["HIVE_SURFACE"] = "terminal"
    if client_ref:
        overrides["CLIENT_REF"] = client_ref
    if owner_type:
        overrides["OWNER_TYPE"] = owner_type
    if owner_ref:
        overrides["OWNER_REF"] = owner_ref
    env.update(overrides)

    # respawn-pane -k replaces the pane's process without touching the
    # window, the session, or any attached client. `-e` per override because
    # the tmux server's own environment predates this conversation.
    # No `=` exact-match prefix here: that syntax is for session targets, and
    # tmux parses a pane target differently.
    args = ["respawn-pane", "-k", "-t", name, "-c", str(PROJECT_DIR)]
    for key, value in overrides.items():
        args.extend(["-e", f"{key}={value}"])
    args.append("--")
    args.extend(cmd)

    result = _tmux(*args, env=env)
    if result.returncode != 0:
        # The pane still holds the old conversation's process, which is the
        # safe failure: the user keeps typing, the context just didn't turn
        # over, and the next Stop hook fire tries again.
        raise RuntimeError(
            f"tmux refused to respawn the pane for session {session_id}: "
            f"{(result.stderr or result.stdout).strip()}"
        )

    log.info("Rotated the conversation in terminal %s onto claude_sid=%s "
             "(seed=%d chars)", name, new_claude_sid, len(system_prompt))
    return True


def spawn_pty(
    session_id: str,
    model: str,
    resume_sid: str | None = None,
    mcp_config: str = "",
    registry: Any = None,
    config_obj: Any = None,
    mind_id: str = "MIND_NAME",
    mind_name: str = "MIND_NAME",
    cols: int = 80,
    rows: int = 24,
    client_ref: str | None = None,
    **kwargs: Any,
) -> tuple[subprocess.Popen, int]:
    """Attach a pty to this session's interactive `claude`, starting it if needed.

    The TUI itself lives in a tmux session named for the hive session and
    outlives every viewer; what this returns is a tmux *client* running in a
    pty of the caller's geometry. Calling it again for a session that already
    has a terminal attaches a second client to the same `claude` rather than
    starting a rival one — `-d` detaches whoever held it before, so one
    conversation always has exactly one keyboard.

    Distinct from `spawn()`: no `-p`, no `--input-format stream-json` — those
    are print-mode flags and disable the TUI (slash commands, tab completion,
    Ctrl+C). Resuming an already-running conversation needs no
    `--append-system-prompt`; that was set on the conversation's first turn.

    ``resume_sid`` is the session's conversation id and is mandatory. The
    mind does not mint conversation ids — the gateway does, once, when the
    session is created, and every surface that opens that session is handed
    the same id. A terminal attach is therefore never "new": it either
    resumes a transcript that exists or pins the harness to the id this
    conversation will have from its first word. Returns the client process
    and the pty master fd; the caller owns both lifecycles, and ending them
    detaches the view without touching the conversation.
    """
    del mind_id, mind_name  # unused, kept for call-site symmetry with spawn()
    owner_type = kwargs.pop("owner_type", None) or ""
    owner_ref = kwargs.pop("owner_ref", None) or ""
    del kwargs  # remainder unused, kept for call-site symmetry with spawn()

    from config import PROJECT_DIR

    if not resume_sid:
        raise ValueError(
            f"spawn_pty for session {session_id} got no conversation id — "
            "the gateway mints one at session creation and must pass it"
        )

    name = tmux_session_name(session_id)
    env = os.environ.copy()
    # Deltas the tmux *server* can't have inherited from us, so they ride on
    # `new-session -e`. Everything else in the pane's environment comes from
    # the server, which this service started and therefore populated.
    overrides = {}
    provider = registry.get_provider(model) if registry else None
    if provider:
        overrides.update(provider.env_overrides)
    # A tile is one session on one conversation; the harness's agent view is a
    # second, conflicting session picker inside it. Left on, `⏴ for agents`
    # backgrounds the conversation into an on-demand daemon that re-hosts it in
    # a nested pty of its own fixed geometry — so the tile renders at a width
    # nobody asked for, and opening another entry forks that transcript into a
    # conversation no session row points at. The sidebar is the session picker.
    overrides["CLAUDE_CODE_DISABLE_AGENT_VIEW"] = "1"
    # A pty spawn is the web terminal by definition — no gateway derivation
    # needed. The surface_inject.sh hook reads this to tell the model which
    # surface each turn arrived on.
    overrides["HIVE_SURFACE"] = "terminal"
    # Per-session env for hooks (rotation_check, etc.) inside the tmux pane.
    # Without these the Stop hook's threshold check finds no CLIENT_REF and
    # bails on every fire, so a terminal conversation never rotates and just
    # grows until Claude's own native compaction is the only thing left to
    # intervene.
    if client_ref:
        overrides["CLIENT_REF"] = client_ref
    if owner_type:
        overrides["OWNER_TYPE"] = owner_type
    if owner_ref:
        overrides["OWNER_REF"] = owner_ref
    env.update(overrides)

    if not pty_session_alive(session_id):
        cmd = _terminal_argv(model, mcp_config, resume_sid, PROJECT_DIR)

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
        log.info("Started tmux session %s (model=%s, claude_sid=%s)", name, model, resume_sid)

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


async def kill(proc: Any) -> None:
    if proc and proc.returncode is None:
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
