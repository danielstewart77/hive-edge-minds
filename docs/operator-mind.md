# Operator mind — install pattern

This document describes how an **operator mind** is installed and run.

An operator mind is a Hive Mind that runs **bare-metal as a single systemd
service on a Linux host**, instead of as a containerised mind in the main
`hive_mind` Docker stack. "Operator" because it operates the host directly:
full filesystem access, full process access, full network access, no
container sandbox between the mind and the machine it lives on.

This is the architecture note for the install type — reusable for any
mind that needs to run with host-level privilege (a system administrator
mind, a workstation-specific mind, a hardware-controller mind, etc.).

> **Why "operator", not "standalone".** The mind does not carry its own
> broker, session manager, or lucent — those live in the shared
> nervous-system containers (hive-comms + hive-lucent, built from the
> upstream hive-mind repo's `nervous-system/` directory), and the mind
> depends on them over HTTP. What's distinctive about this install type
> is host-level access, not isolation.

## When this install type fits

Use this layout when you want a mind that:

- Runs as a single systemd service directly on the host (not in Docker)
- Has full host access by design — filesystem, network, processes
- Stays available while the host's nervous-system containers are
  restarting
- Connects to a shared nervous system (broker, session manager, secrets,
  memory) over HTTP rather than carrying its own

If those constraints don't apply, the regular containerised mind layout
in the upstream `hive_mind` repo is the better default.

## Architecture in one diagram

```
                 ┌─────────────────────────────────────────────────────┐
                 │   <name>.service  (systemd, one Python process)     │
                 │                                                     │
   Telegram ───► │  bots.telegram_bot ──► COMMS_URL (hive-comms NS)    │
                 │                              │                      │
                 │                              │  HTTP + bearer       │
                 │                              ▼                      │
                 │                       mind_server.app               │
                 │                       (FastAPI on MIND_SERVER_PORT) │
                 │                              │                      │
                 │                              │  spawns              │
                 │                              ▼                      │
                 │                       claude CLI subprocess         │
                 │                                                     │
                 │  Stop hook ──► auto_remember.sh ──► LUCENT_URL_SELF │
                 │  (~/.claude/hooks/)                                 │
                 └────────────────────────────────────┬────────────────┘
                                                      │
       ┌──────────────────────┬──────────────────────┼─────────────────────────┐
       │                      │                       │                         │
       ▼                      ▼                       ▼                         ▼
   hive-comms              hive-lucent            hive-tools              voice-server
   (NS broker +            (vector + KG)          (Ollama)                (STT / TTS)
    sessions +              HTTP + bearer          HTTP + bearer           HTTP
    secrets +
    bootstrap_loader)
```

Everything to the left of the dashed line is local to the mind's process.
Everything to the right is an external nervous-system service the mind
talks to over HTTP. The mind owns no state of its own beyond `data/`
(sessions + broker SQLite) and `.env` (secrets); identity, soul, memory,
and prompt composition all live in the NS.

## Companion files

- [`operator-mind/launch_mind_server_and_bots.py`](operator-mind/launch_mind_server_and_bots.py)
  — reference launcher. Copy verbatim to
  `<INSTALL_PATH>/launch_mind_server_and_bots.py`.
- [`operator-mind/architecture-notes.md`](operator-mind/architecture-notes.md)
  — deep technical detail on the sharp edges this layout works around.

## Prerequisites

### Local

- Python 3.12+
- Systemd
- A clone of this repo at the install path
- `python3-venv` installed
- `claude` CLI installed and on the run user's `$PATH` (typically
  `~/.local/bin/claude` — captured in the systemd unit's `Environment=PATH`)
- The mind's own files scaffolded by `/create-mind`:
  `minds/<name>/implementation.py`, `minds/<name>/MIND.md`, `souls/<name>.md`

### External services (must be reachable)

- **hive-comms** at `COMMS_URL` — owns the broker, session manager, secrets
  API, bootstrap_loader, and prompt composition. Composes
  `system_prompt_blocks` (soul + decay-weighted recent + session-memory)
  per dispatch. Standing rules are injected separately, per turn, by the
  `contextual_retrieval.py` hook.
- **hive-lucent** at `LUCENT_URL_SELF` — vector store + knowledge graph.
  Built from the upstream hive-mind repo's `nervous-system/` directory.
- **hive-tools** at `HIVE_TOOLS_URL` — Ollama classifier used by memory
  hooks.
- **voice-server** at `VOICE_SERVER_URL` — STT/TTS for voice-enabled
  surfaces. Optional unless any surface needs voice.

All three services use bearer-token auth. Tokens go in `.env`.

## Install steps

### 1. Python venv + dependencies

```bash
cd <INSTALL_PATH>
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 2. OAuth credentials (Claude CLI harness)

The mind authenticates against the run user's existing Claude installation
at `$HOME/.claude/`. Set `CLAUDE_CONFIG_DIR=/home/<run_user>/.claude` in
`.env` (see §4) and run the systemd unit as `User=<run_user>`.

> **Why explicit?** `mind_server.py` reads `CLAUDE_CONFIG_DIR` at
> module-import time and `mkdir(parents=True, exist_ok=True)`s on it. The
> default (`/home/hivemind/.claude-config`) is the Docker container's path
> and `PermissionError`s on bare-metal. Pointing it at the host user's
> actual `.claude/` makes the import-time mkdir a no-op. See
> [`architecture-notes.md` §1](operator-mind/architecture-notes.md#1-claude_config_dir-and-_setup_config_dir-at-import-time).

### 3. config.yaml

Minimum viable config:

```yaml
server_port: 8431                # legacy gateway port (no longer in-process; kept for future use)
mind_server_port: 8421
idle_timeout_minutes: 30
default_model: opus

providers:
  anthropic: {}

models:
  sonnet: anthropic
  opus: anthropic
  haiku: anthropic

# Telegram bot — required when wiring the Telegram surface (§5).
# Empty list = bot rejects every incoming message.
telegram_allowed_users:
  - <user_telegram_id>
telegram_owner_chat_id: <user_telegram_id>
```

### 4. `.env`

Mode 600, gitignored. Carries every secret.

```bash
# Identity
MIND_NAME=<name>                                  # human-readable, lowercase
MIND_ID=<uuid>                                    # generate once with `uuidgen`
MIND_SERVER_PORT=8421

# External services (hive-comms / lucent / hive-tools)
COMMS_URL=http://127.0.0.1:8426
COMMS_BEARER_TOKEN=<token>
LUCENT_URL_SELF=http://127.0.0.1:8425
LUCENT_BEARER_TOKEN=<token>
HIVE_TOOLS_URL=http://127.0.0.1:9421
HIVE_TOOLS_TOKEN=<token>

# Legacy — mind_server.py's /secrets/... fetch still reads this. Point it
# at hive-comms (same as COMMS_URL). Will be cleaned up; see
# architecture-notes §2.
HIVE_MIND_SERVER_URL=http://127.0.0.1:8426

# Surfaces
TELEGRAM_BOT_TOKEN=<bot-token-from-BotFather>
VOICE_SERVER_URL=http://<voice_host>:<voice_port>  # optional

# Claude CLI auth
CLAUDE_CONFIG_DIR=/home/<run_user>/.claude
```

**MIND_ID is a UUID, not the name.** Every write to lucent stamps
`mind_id=<uuid>` on the row, and lucent's identity guard rejects writes
to the Mind node unless they match. `MIND_NAME` stays as the display
label. See [CLAUDE.md "Identity convention"](../CLAUDE.md#identity-convention).

### 5. The launcher and the systemd unit

Copy the reference launcher
[`operator-mind/launch_mind_server_and_bots.py`](operator-mind/launch_mind_server_and_bots.py)
to `<INSTALL_PATH>/launch_mind_server_and_bots.py`. It imports
`mind_server.app` and `bots.telegram_bot.run_telegram_bot`, then runs them
with `asyncio.gather()`.

If you add more surfaces (Discord, additional bots, etc.), gather them as
additional coroutines in the same file.

**`/etc/systemd/system/<name>.service`:**

```ini
[Unit]
Description=<name> — Hive Mind Operator Mind (bare-metal Linux service)
After=network.target

[Service]
Type=simple
User=<run_user>
WorkingDirectory=<INSTALL_PATH>
EnvironmentFile=<INSTALL_PATH>/.env
Environment=PATH=/home/<run_user>/.local/bin:/home/<run_user>/.nvm/versions/node/<node_ver>/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=<INSTALL_PATH>/.venv/bin/python3 launch_mind_server_and_bots.py
Restart=no
KillSignal=SIGTERM
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> **Why `Environment=PATH=...`?** systemd's default PATH does not include
> `~/.local/bin` where the Claude CLI lives. Without the override,
> `mind_server` fails to spawn sessions with
> `[Errno 2] No such file or directory: 'claude'`. Use `which claude` to
> find the actual path; include Node's bin if your Claude CLI version
> needs it.

> **`Restart=no` fits an "elder mind, dormant until summoned" pattern** —
> you `start` the mind when needed and let it exit on error so you
> notice. For an operator mind that should always be up, use
> `Restart=on-failure`.

Apply:

```bash
sudo systemctl daemon-reload
sudo systemctl start <name>.service
curl -sf http://localhost:<MIND_SERVER_PORT>/health
```

### 6. Mind identity scaffolding

Three pieces:

**(a) MIND.md frontmatter** at `minds/<name>/MIND.md`:

```yaml
---
name: <name>
mind_id: <uuid>                         # same UUID as MIND_ID in .env
model: opus
harness: claude_cli
gateway_url: http://localhost:<MIND_SERVER_PORT>
deployment: bare-metal
install_path: <INSTALL_PATH>
mind_server_port: <MIND_SERVER_PORT>
auth: oauth
soul_file: souls/<name>.md
---
```

> **About `gateway_url`.** It points at the mind_server port. The gateway
> itself lives in hive-comms and is reached via `COMMS_URL`, so this
> field unambiguously addresses the mind backend.
> See [`architecture-notes.md` §3](operator-mind/architecture-notes.md#3-the-gateway_url-field-is-a-historical-artifact).

**(b) Prompt composition is owned by hive-comms.** This mind composes
nothing locally. At spawn time, hive-comms
`bootstrap_loader.compose_prompt_blocks` queries lucent for soul +
decay-weighted recent + session carry-forward, composes those three
blocks, and ships the string in the dispatch payload as
`system_prompt_blocks`. `mind_server` passes it straight to
`claude --append-system-prompt`. (Standing rules are not composed here —
the per-turn `contextual_retrieval.py` hook injects them.)

**(c) Soul seeded into the lucent KG.** `souls/<name>.md` is a seed file —
the running mind reads its soul from a `Mind` node in the graph, not the
file.

Seed once during install via the `/seed-mind` skill (or hand-roll an HTTP
upsert to `LUCENT_URL_SELF/graph/upsert`). The required node:

- `type: Mind`
- `name: <Name>` (capitalised)
- `mind_id: <uuid>` (the MIND_ID from `.env`)
- `properties.soul_values`: a list of strings, one entry per non-empty
  line of the soul file, verbatim

`bootstrap_loader` reads `soul_values` fresh on every dispatch — no
service restart needed after seeding.

## Surfaces

### Telegram

The Telegram bot ships embedded in `bots/telegram_bot.py` and is gathered
automatically by `launch_mind_server_and_bots.py`. No refactor needed; it
already exports `run_telegram_bot()` as an asyncio coroutine that handles
its own `Application` lifecycle.

**Token + allowlist:** put `TELEGRAM_BOT_TOKEN` in `.env`; put allowed
user IDs in `config.yaml`'s `telegram_allowed_users`. Empty list rejects
every message (including yours). Per-host isolation comes from each
mind's own `.env` — no shared store.

**Error visibility:** the handlers reply with the actual exception text
(truncated to 3.5k chars) so failure modes surface in chat rather than
only in the journal. See
[`architecture-notes.md` §4](operator-mind/architecture-notes.md#4-telegram-bot-error-reporting).

### Adding more surfaces

Add the new coroutine import and gather it in
`launch_mind_server_and_bots.py`. Env-gate it if it should be optional:

```python
coros = [mind_server.serve(), run_telegram_bot()]
if os.environ.get("DISCORD_BOT_TOKEN"):
    from bots.discord_bot import run_discord_bot
    coros.append(run_discord_bot())
await asyncio.gather(*coros)
```

## Hooks (memory capture + retrieval)

Memory is captured per turn by hooks installed at the Claude Code
user-config level (`~/.claude/hooks/`). Edit IS deploy — no staging
mirror; the live hook scripts are the canonical source.

Key hooks:

- `auto_remember.sh` — Stop hook. Extracts last user/assistant turn from
  the transcript, classifies via hive-tools `/ollama/structured`, posts
  to `LUCENT_URL_SELF/memory/store` with
  `tier=contextual, source=session`.
- `contextual_retrieval.sh` — UserPromptSubmit hook. Looks up similar
  memories for the current turn.
- `time_inject.sh` — UserPromptSubmit hook. Injects fresh date stamp per
  turn.
- `rotation_check.py` — Stop hook. Writes new session_memory rows to
  hive-comms on rotation.

The hooks read `MIND_ID` (the UUID, not the name) when stamping any
`mind_id=…` field on the lucent API.

## Verification — smoke test

```bash
# 0. Service is active
systemctl is-active <name>
# active

# 1. Mind backend up
curl -sf http://localhost:<MIND_SERVER_PORT>/health
# {"mind_id":"<uuid>","status":"ok","sessions":0}

# 2. External services reachable from this host
curl -sf -H "Authorization: Bearer ${COMMS_BEARER_TOKEN}" $COMMS_URL/health
curl -sf -H "Authorization: Bearer ${LUCENT_BEARER_TOKEN}" $LUCENT_URL_SELF/health
curl -sf -H "Authorization: Bearer ${HIVE_TOOLS_TOKEN}"  $HIVE_TOOLS_URL/health

# 3. Mind registered with the comms broker
curl -sf -H "Authorization: Bearer ${COMMS_BEARER_TOKEN}" \
  $COMMS_URL/broker/minds | jq

# 4. Soul seeded into lucent KG
curl -sf -H "Authorization: Bearer ${LUCENT_BEARER_TOKEN}" \
  "$LUCENT_URL_SELF/graph/query?name=<Name>&mind_id=$MIND_ID" | jq '.nodes[0].properties.soul_values | length'
# Expect: an integer > 0 matching the number of non-empty lines in souls/<name>.md

# 5. Telegram bot started
journalctl -u <name>.service --no-pager | grep -E "Telegram bot started"

# 6. Round-trip — DM the bot from an allowed user. Handlers surface real
#    exceptions in chat, so failure modes are visible without journalctl.
```

## Notes

- **Single failure domain.** When any in-process coroutine raises,
  `asyncio.gather()` propagates and the whole process exits. systemd's
  `Restart=` decides whether it comes back.
- **One journal stream.** `journalctl -u <name>.service -f` shows
  mind_server and bot logs interleaved.
- **Port uniqueness.** Pick `MIND_SERVER_PORT` clear of any other
  hive_mind installs or the main NS stack (`ss -tln`).
- **Mind binds 0.0.0.0.** Firewall the mind_server port if not on a
  trusted LAN. The mind expects to be reached only by the local
  hive-comms or local loopback.
- **Stale `__pycache__`.** Under `minds/<name>/` it can mask source
  changes if you're moving files. Delete and let Python regenerate.
