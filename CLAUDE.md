# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

**hive-edge-minds** is a single-mind installation of the
[hive-mind](https://github.com/danielstewart77/hive-mind) system: one AI mind
on one machine, connected to a hive's shared services (the `hive-comms`
gateway and `hive-lucent` memory containers) over HTTP+bearer. The repo ships
machinery only — a mind's identity (`minds/<name>/`, `souls/<name>.md`,
`.env`, `config.yaml`) is per-host and gitignored.

An edge mind declares a **role** (`operator` — full host access by design;
`sandboxed` — connected but not operating the machine) and a **deployment**
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
                │  the edge mind  (one Python process)             │
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

There are two templates, not one per harness-and-provider pair. The provider
is a separate axis, named in `runtime.yaml` and resolved through
`config.yaml`'s `providers:` block: `ModelRegistry.get_provider(model)` returns
the env overrides each spawn applies. An Ollama-backed mind is the same
harness with a different provider, and keeps its browser terminal — which is
why no `*_ollama` template exists to lose it.

- `claude_cli` — long-lived `claude --stream-json` per session, plus
  `spawn_pty` for the browser terminal. Ollama needs nothing beyond the
  provider's `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` overrides.
- `codex_cli` — one `codex exec --json` subprocess per turn, plus
  `spawn_pty` for the browser terminal. Codex has no base-URL environment
  variable, so a non-default provider is declared as a
  `model_providers.<mind>_ollama` config block and selected with
  `model_provider` — `_provider_args` builds those `-c` flags, and the pane
  caches them because a rotation respawns it with no registry in hand.
  `env_key` is declared only when a key is actually configured: bare Ollama
  needs no auth, a metering proxy in front of it does.
  Codex mints its own thread ids and
  cannot adopt the gateway's conversation id; hive-comms persists that
  provider-native id as `harness_sid`, while the edge mind keeps a local
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

### runtime.yaml is read every boot, and written over HTTP

`minds/<name>/runtime.yaml` is the durable truth about a mind; the broker's
`minds` row is a cache of it. `mind_server` re-registers from the file on
every start (`runtime_config.registration_payload` → `POST /broker/minds`,
an upsert on `mind_id`), which is what makes the file authoritative rather
than an install-time artifact. Registration failure is logged, never fatal.

`GET /runtime` reports the allowlisted configuration; `PATCH /runtime` sets
`default_model` by rewriting that one line in place (line substitution, not
a YAML round-trip — a dump would strip the comments). The write is guarded
by `MIND_ADMIN_TOKEN`, falling back to `COMMS_ADMIN_BEARER_TOKEN`; neither
configured means the route refuses with 503 rather than opening. The hive
console uses these two routes for every mind — a container in the stack, a
bare-metal mind here, or a mind on another machine — writing the file first
and the broker row second, so a restart can't undo the edit.

The model itself is never defaulted. A spawn or an `attach-pty` that arrives
without one is refused: comms resolves it per session from the broker row,
and a mind quietly substituting a house favourite is how a wrong model goes
unnoticed for weeks. A rotation carries the conversation's own model
forward — the pty handle remembers what its pane started on — so editing a
mind's default never moves a live conversation. That default is for the next
conversation.

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
launches `codex resume <id>` against that discovered id — unless that
thread's rollout no longer exists under this `CODEX_HOME` (a migration or
a redeploy onto a fresh volume left `harness_sid` and the disk safety copy
pointing at a thread minted elsewhere), in which case `_rollout_exists`
discards it and the terminal falls back to the fresh-terminal path instead
of handing back a pane that dies within a second of tmux starting it.
`app-server` has its own arg parser and rejects `--profile` outright —
unlike plain `codex`/`codex exec` invocations — so both paths read the
config profile from `CODEX_HOME`'s `config.toml` directly instead.

The process ends only on `DELETE /sessions/{id}` or via the idle reaper
(`PTY_IDLE_TIMEOUT_SECONDS`, default one hour unattached). A turn in flight
survives a closed tab.

A rotation replaces the conversation, not the session and not the terminal.
The session row is permanent — the tile, its label, the turn ledger and the
`active_sessions` binding are all keyed to `sessions.id` — so what rotates
under it is the harness conversation. Rotation normally arms a flag that
hive-comms consumes on the conversation's next user turn, but keystrokes are
raw bytes: the terminal never calls `send_message`, so there is no turn to
consume it. For sessions whose `owner_ref` is `terminal`, `arm_rotation`
rotates on the spot. It composes the carry-forward, mints a new `claude_sid`
and calls `POST /sessions/{id}/rotate-pty` on the mind; `rotate_pty_session`
`respawn-pane -k`s the pane onto a fresh harness process carrying that
carry-forward, renaming nothing and killing nothing, so the attached tmux
client — and with it the pty, the proxied socket and the browser tile — is
never disturbed. `mind_server` repoints the pty handle's `claude_sid`, and
hive-comms writes the same id onto the same row. The user keeps typing in
the same pane under the same session id while the context behind it resets.
Nothing is published and the tile is told nothing, because nothing it tracks
changed. tmux targets are prefix-matched, so every session lookup uses the
`=` exact-match form; without it one id can answer for another's pane.

The carry-forward travels in a file, never in the tmux command: a composed
prompt is tens of thousands of characters, tmux rejects a long
`respawn-pane` with "command too long", and Linux caps one argv entry at
`MAX_ARG_STRLEN` (128 KiB) regardless. The pane runs a one-line `sh -c` that
reads the seed, deletes it and `exec`s the harness with it; `_capped_seed`
trims anything past 120,000 chars to its tail. A mind reporting no live
terminal writes nothing at all — a fresh `claude_sid` with no process behind
it would strand the session on a conversation that was never seeded.

Chat-surface rotation still retires the session for a successor row (there
is no pane to respawn), and `kill_session` publishes `rotated_to` on the
`session_closed` event so an attached observer reattaches via close code
**4412** with the successor id as the reason. Bare 4410 keeps its meaning:
ended, no successor.

Terminal turns also have to reach the `session_turns` ledger, which is what
`GET /sessions/late-turns` reads to fold turns typed during the rotation's
multi-minute background window into the new conversation's
`<pending-continuation>`.
`send_message` writes that ledger for chat surfaces; the pty bridge can't, so
the Stop hook POSTs each completed turn to `POST /sessions/record-turn` on
every fire when `HIVE_SURFACE=terminal`.

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

### Project instructions per harness

Claude reads `CLAUDE.md`; codex reads `AGENTS.md` and ignores `CLAUDE.md`
entirely. `AGENTS.md` is therefore a tracked symlink to `CLAUDE.md` — one
source, both harnesses. Codex resolves no `@path` imports inside it, so there
is no split-file equivalent of `CLAUDE.local.md` at the repo root; a codex
mind's per-install notes go in `$CODEX_HOME/AGENTS.md`, which codex loads
alongside the repo file.

### Skill sync

A skill exists twice: `specs/skills/<harness>/` is the tracked source that
travels with a clone, split into `claude/` and `codex/` subdirectories
because Codex honours a subset of Claude's frontmatter — the files could be
shared, but writing them separately keeps each one honest about what its
harness actually reads. The harness reads a different directory entirely
(`$CLAUDE_CONFIG_DIR/skills` or `$CODEX_HOME/skills`), and that installed
copy is the one that runs. Nothing keeps the two in step, so a mind drifts
from the repo the moment anyone edits a skill in place — a feature, since
that is how a mind gets a skill tuned to its own job.

`skills_sync.py` is the mind's own view of both sides, since only the mind's
own filesystem can see both. `mind_server.py` exposes it as `GET /skills`
(one row per skill, reporting `same` / `differs` / `not_installed` /
`local_only` / `unreadable`), `GET /skills/{name}/diff` (unified diff, repo
against installed), `POST /skills/{name}/install` (copy repo onto mind —
apply and revert are the same operation), `POST /skills/{name}/write-back`
(copy mind onto repo; nothing is committed, the write lands in a working
tree still needing review and push), and `DELETE /skills/{name}` (remove the
mind's copy only). Same shape as `runtime.yaml`'s registration: a container,
a bare-metal mind, and a mind on another machine are one code path because
the mind reports its own state rather than a bind mount reaching in.

Every route is admin-guarded, reads included: the listing returns the full
text of every skill the mind runs, on a port reachable across the LAN.

State is a hash of the whole skill directory, not of `SKILL.md`. A skill is
a directory — references, scripts and templates travel with the markdown,
and a copy moves all of it — so comparing only the markdown would call a
skill synced while its scripts had drifted, and the console offers no action
on a synced row. For the same reason `unreadable` is its own state rather
than folding into "absent": the remedy offered for absence is to overwrite
the directory, and doing that to a skill that is merely unreadable destroys
it. A directory the harness cannot list raises rather than returning empty,
because "this mind has no skills" is a sentence the console will otherwise
state as fact.

Copies preserve symlinks and refuse a tree past `MAX_SKILL_BYTES`.
Dereferencing turns a skill carrying a virtualenv into hundreds of megabytes
of materialised interpreter, which a `venv/` gitignore then hides from `git
status` entirely. Staging is a unique temporary directory inside the target,
so two writers sharing one skills directory cannot delete each other's
staging mid-copy and each report success over a truncated tree.

What the harness does with a newly-written skill is the harness's business.
This machinery copies a directory into the one the harness reads; it makes
no claim about when that skill becomes loadable.

### What the repo ships

`specs/skills/<harness>/` carries the skills without which a mind is not a
mind: `self-reflect` (identity from the graph), `rotate-session`,
`end-session`, `save-session`, `remember`, `always-remember` and `memory`.
Everything else — a host's hardware, a person's projects, one machine's
integrations — belongs only in the installed directory and never here.

A shipped skill names no mind and no person. It reads `$MIND_NAME`,
`$MIND_ID` and the lucent and comms variables from the environment the
service unit already provides, and says "the operator" in prose. Anything
else is a skill exactly one install can run.

There are no `{{PLACEHOLDER}}` tokens and no install-time substitution step.
Substitution would leave every installed copy differing from its source
forever, and the whole point of the sync is that `same` means same.

A skill needing repo content — the data-class specs, a stateless tool —
resolves it through `HIVE_PROJECT_DIR`, which `setup.sh` stamps into `.env`
with this checkout's absolute path. A skill needing a helper script carries
it inside its own directory (`remember/remember.sh`), which works because a
skill is a directory and the sync copies all of it. A script in a shared
`scripts/` folder does not travel and is how a skill arrives broken.

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
