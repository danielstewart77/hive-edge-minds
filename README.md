# hive-edge-minds

One mind, connected to a hive.

An **edge mind** is a single-mind installation of the
[Hive Mind](https://github.com/danielstewart77/hive-mind) system: one AI mind
running on a machine of your choosing — bare-metal Linux, a container, or a
Windows box — wired back to a hive's nervous system for sessions, memory, and
inter-mind messaging. The hive runs the shared services; the edge mind runs the
mind.

```
                ┌─────────────────────────────────────────────────┐
                │  the edge mind  (one process on your machine)   │
                │                                                 │
   Telegram ──► │  telegram_bot ──► COMMS_URL ──► mind_server     │
                │                                      │          │
                │                                      │  spawns  │
                │                        claude / codex subprocess│
                │                                                 │
                │  memory hooks ──► HTTP + bearer ──► the hive    │
                └──────────────────────┬──────────────────────────┘
                                       │
                ┌──────────────────────▼──────────────────────────┐
                │  the hive  (hive-mind repo, Docker)             │
                │  hive-comms: sessions · broker · HITL           │
                │  hive-lucent: vector store · knowledge graph    │
                └─────────────────────────────────────────────────┘
```

## Roles

Every edge mind declares what it is allowed to be on its machine:

- **operator** — full host access by design. The mind operates the machine:
  filesystem, processes, Docker, the lot. For a system-administrator mind on
  a workstation or server you own.
- **satellite** — a connected mind without the operator posture. For a family
  member's PC, a secondary box, or any machine where the mind should
  converse and remember but not run the place.

Deployment is orthogonal to role: either role can run as a **systemd**
service, a **container**, or a **windows-task** (logon-triggered scheduled
task).

## Profiles

Every installation also declares a deployment profile:

- **standard** — the generic one-mind runtime in this repository.
- **sentinel** — the same runtime plus the separately managed
  `hive-sentinel` security logic, telemetry collectors, Loki, and Grafana.

The profile records what belongs to the installation without mixing
specialized Sentinel code into the generic Edge Mind runtime.

## Prerequisites

- A running hive: the `hive-comms` and `hive-lucent` containers from the
  [hive-mind](https://github.com/danielstewart77/hive-mind) repo, reachable
  over HTTP from this machine, with bearer tokens for both.
- A harness CLI on this machine: [Claude Code](https://claude.com/claude-code)
  (`claude`) or Codex (`codex`), logged in.
- Python 3.12+. For systemd Claude minds: `tmux` (the browser terminal runs
  each session inside it).
- A Telegram bot token from @BotFather (each mind needs its own bot), if the
  telegram surface is enabled.

## Quickstart

```bash
git clone https://github.com/danielstewart77/hive-edge-minds
cd hive-edge-minds
./setup.sh
```

The wizard asks for a name, profile, role, deployment, harness, and surfaces,
then scaffolds the mind (`minds/<name>/`, `souls/<name>.md`), stamps `.env`,
and emits the installer for your deployment into `deploy/` (or
`docker-compose.yml`). It never runs sudo and never starts anything; it
prints the install commands and leaves the moment to you.

Unattended:

```bash
./setup.sh --name atlas --role operator --deployment systemd \
           --harness claude_cli_claude --surfaces telegram --port 8421
```

Then fill in `.env` (tokens), edit `souls/<name>.md`, run the printed
install commands, and register the mind with your hive (`/add-mind` in the
hive-mind repo).

## What's in the box

```
hive-edge-minds/
├── setup.sh                        # the wizard — scaffold + installer emission
├── launch_mind_server_and_bots.py  # entry point: mind server + surface bots, one process
├── launch_windows.py               # Windows Task Scheduler bootstrap
├── mind_server.py                  # the mind's local backend (HTTP + browser-terminal WS)
├── mind_templates/                 # harness adapters (claude_cli, codex_cli, ...)
├── minds/example/                  # the contract: runtime.yaml + implementation.py
├── souls/example.md                # soul seed template
├── bots/                           # surface clients (telegram, discord)
├── voice/                          # voice server + wake-word desktop app
├── tools/stateless/                # standalone scripts invoked via skills
├── docs/                           # memory system, operator pattern, guides
├── specs/                          # agent-facing specifications
└── tests/                          # pytest suite
```

Your mind's identity — `minds/<name>/`, `souls/<name>.md`, `.env`,
`config.yaml` — is gitignored. The repo ships machinery, never identity;
pulling updates never touches who your mind is.

## The mind contract

`minds/<name>/runtime.yaml` is the one file that says what a mind is:

```yaml
name: atlas
mind_id: 2f1c9e58-...        # generated once; every memory write carries it
role: operator                # operator | satellite
deployment: systemd           # systemd | container | windows-task
harness: claude_cli_claude    # mind_templates/ basename
provider: anthropic
default_model: sonnet
mind_server_port: 8421
surfaces: [telegram]
soul_file: souls/atlas.md
```

`mind_id` and `name` are not interchangeable: the UUID is the identity every
write to the hive's memory carries; the name is a display label and is never
written there.

## Surfaces

- **Telegram** — the primary conversational surface. Runs in-process;
  streams replies, voices them when `ALWAYS_VOICE=1`, and supports session
  pickup across surfaces (`/sessions`, `/switch`).
- **Browser terminal** — a raw interactive `claude` in the browser
  (xterm.js over the `attach-pty` WebSocket route), tmux-backed so
  conversations survive closed tabs. Claude harness only.
- **Discord** — optional second chat surface.
- **Wake-word desktop app** — a kid-friendly smart-speaker window
  (`launch_listener.py`) for Windows satellite minds with a microphone.

## Memory

Sessions are throwaway; the hive's knowledge graph and vector store carry
identity and memory across them. Capture is per-turn via harness hooks, and
prompt composition happens hive-side — see [`docs/memory-system/`](docs/memory-system/).

## Federation

Connecting an edge mind to a second hive is specified but not yet implemented —
see [`specs/federation.md`](specs/federation.md).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

## License

See [LICENSE](LICENSE).
