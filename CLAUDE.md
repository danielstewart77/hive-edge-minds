# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

**hive-outpost** is a single-mind installation of the
[hive-mind](https://github.com/danielstewart77/hive-mind) system: one AI mind
on one machine, connected to a hive's shared services (the `hive-comms`
gateway and `hive-lucent` memory containers) over HTTP+bearer. The repo ships
machinery only — a mind's identity (`minds/<name>/`, `souls/<name>.md`,
`.env`, `config.yaml`) is per-host and gitignored.

An outpost declares a **role** (`operator` — full host access by design;
`satellite` — connected but not operating the machine) and a **deployment**
(`systemd`, `container`, or `windows-task`). `setup.sh` scaffolds the mind
from `minds/example/` and emits the matching installer. It never runs sudo
and never starts anything.

### Process model

One deployment unit runs **one Python process**
(`launch_mind_server_and_bots.py`), hosting the mind backend and the surface
bots in-process via `asyncio.gather()`:

- `mind_server.app` — the mind's local backend (HTTP + pty-attach WS)
- `bots.telegram_bot` / `bots.discord_bot` — surface clients

```
                ┌──────────────────────────────────────────────────┐
                │  the outpost  (one Python process)               │
                │                                                  │
   Telegram ──► │  telegram_bot ──► COMMS_URL ──► mind_server.app  │
                │              (hive-comms)            │           │
                │                                      │  spawns   │
                │                       claude / codex subprocess  │
                │                                                  │
                │  memory hooks ──► HTTP+bearer ──► hive-lucent    │
                └──────────────────────────────────────────────────┘
```

`mind_server.py` is pure spawn/IO — **no prompt composition in the mind**.
hive-comms composes the system prompt (soul from the KG + decay-weighted
recent + session-memory carry-forward) and ships it as
`system_prompt_blocks` in the dispatch payload; the mind passes it straight
to the harness. Standing rules and contextual memory ride in per turn via
UserPromptSubmit hooks configured at the harness user-config level.

### Harness adapters

`mind_templates/<harness>.py` is instantiated to
`minds/<name>/implementation.py` by `setup.sh` (a `MIND_NAME` string
substitution). `mind_server` loads `minds.$MIND_NAME.implementation` and
routes by shape: a `send` coroutine plus session-id `kill` means the
per-turn path (codex); a long-lived process from `spawn` means the
stream-json path (claude).

- `claude_cli_claude` — long-lived `claude --stream-json` per session, plus
  `spawn_pty` for the browser terminal.
- `codex_cli_codex` — one `codex exec --json` subprocess per turn, plus
  `spawn_pty` for the browser terminal. Codex mints its own thread ids and
  cannot adopt the gateway's conversation id; hive-comms persists that
  provider-native id as `harness_sid`, while the outpost keeps a local
  disk-backed safety copy. A failed or incomplete turn clears both so the
  next turn never resumes a broken one. POSIX spawns use `start_new_session`
  + `killpg`
  (the node wrapper's rust child must not orphan to PID 1); Windows uses
  CREATE_NO_WINDOW (hidden console inherited by children — never
  DETACHED_PROCESS, which makes every console child pop a visible conhost
  window).

### Conversation ids have exactly one origin

hive-comms mints the conversation id when the session row is written. Every
spawn is handed that id: `--resume` when a transcript exists, `--session-id`
when it's the conversation's first process. A mind handed no id raises
rather than inventing one. Surfaces therefore share one conversation by
construction. Codex additionally reports its own thread id, which hive-comms
stores separately as `harness_sid` and returns on every spawn and terminal
attach; the session's identity remains the gateway's id.

### Browser terminal (tmux-backed)

`mind_server.py` exposes `WS /sessions/{id}/attach-pty`, bridging raw bytes
between an xterm.js tile and an interactive harness CLI (`claude` or
`codex`, per the mind's template — both templates implement `spawn_pty`).
**The conversation lives in tmux; a tile is a client.** `spawn_pty` starts
the CLI inside a tmux session on a dedicated socket, then attaches a `tmux
attach-session -d` client in a pty of the tile's geometry — ending the
client detaches the view without touching the conversation, and
re-attaching joins the same session rather than starting a rival CLI
process. tmux owns the screen model and history: it repaints on attach and
on live resize, which is why no scrollback ring, VT emulator, or snapshot
painter exists here. `_take_controlling_tty` (setsid + TIOCSCTTY) makes
SIGWINCH actually reach the app — without a controlling terminal a resize
sets winsize and signals nobody. The socket carries a NUL-byte heartbeat
every 5s so half-open mobile connections are detectable. For the claude
harness, `CLAUDE_CODE_DISABLE_AGENT_VIEW=1` is set in the pane: the
harness's agent view is a second session picker inside a surface that
already has one, and it re-hosts the conversation in a nested pty at the
wrong geometry. For the codex harness, a fresh terminal launches bare
`codex` (no thread id yet) rather than pre-minting one through `app-server`:
`app-server`'s `thread/start` RPC returns a thread id and an intended
rollout path without actually writing that rollout file to disk, so
`codex resume <id>` against a pre-minted id fails with "No saved session
found." A background daemon thread polls `CODEX_HOME/sessions/` for the one
new rollout file codex itself writes once the user's first real turn
begins, extracts the thread id from its filename, and reports it via the
same `harness_sid` path the per-turn chat flow uses — mirroring how that
flow already captures `thread.started` in real time. Every later reattach
launches `codex resume <id>` against that discovered id. `app-server` has
its own arg parser and rejects `--profile` outright — unlike plain
`codex`/`codex exec` invocations — so both paths read the config profile
from `CODEX_HOME`'s `config.toml` directly instead.

The process ends only on `DELETE /sessions/{id}` or via the idle reaper
(`PTY_IDLE_TIMEOUT_SECONDS`, default one hour unattached). A turn in flight
survives a closed tab.

### Cross-surface session pickup

Telegram's `/sessions` lists conversations another surface is holding
(flagged with the surface label); `/switch` adopts one: the mind releases
the terminal (`POST /sessions/{id}/release?surface=terminal` kills the tmux
session and nothing else), ownership retargets, and the stream-json process
respawns with `--resume`. The transcript *is* the handover. The rule
underneath is one live harness process per conversation. Telegram turns
that arrive while a terminal tile is open are mirrored into the attached
socket by `_mirror_turn_to_pty` (a live overlay; tmux can't be told about
bytes it didn't produce).

## Identity convention

| Variable | Purpose |
|---|---|
| `MIND_NAME` | Display label for logs, paths, tmux session names. **Never written to the hive's memory.** |
| `MIND_ID` | UUID generated once by `setup.sh`, stamped on the mind's KG node. The `mind_id` field on every memory write. |

## Memory architecture

Sessions are throwaway; the hive's KG and vector store carry weight across
rotations. Capture is per-turn via a Stop hook (pure bash + jq + curl,
detached subshell); retrieval is three-tier (standing / initial prime /
contextual per-turn). Hooks live at the harness user-config level
(`~/.claude/hooks/`), not in this repo. Full design:
[`docs/memory-system/`](docs/memory-system/). Data classification:
[`specs/data-classes/`](specs/data-classes/).

## Key design principles

1. **One process, not many** — simpler ops, one log stream.
2. **Memory is the continuity layer** — the Stop hook is the orchestrator.
3. **Hooks > orchestrators** — bash + jq + curl beats a subagent triad for
   per-turn capture.
4. **Per-process env isolation** — env vars set per subprocess, never
   globally.
5. **Harness-native operations first** — before writing code, ask whether
   Bash / Edit / Write / curl / sqlite3 / docker can do it directly. See
   [`specs/harness-native-operations.md`](specs/harness-native-operations.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest        # zero failures is the bar
```

Tests land with code changes — not optional, not a follow-up.

## Adding new tools

Preferred pattern is **stateless**: a standalone script under
`tools/stateless/<name>/` with argparse + JSON stdout, wired to the harness
via a skill. Editable without restart, no process state, no registration
step. Only reach for a persistent service when the tool genuinely needs a
long-lived connection.
