# Configuration

## config.yaml

Non-secret settings live in `config.yaml`. Copy `config.yaml.example` to get started.

```yaml
server_port: 8420
idle_timeout_minutes: 30
max_sessions: 10
default_model: sonnet

providers:
  anthropic: {}
  ollama:
    env:
      ANTHROPIC_AUTH_TOKEN: "ollama"
      ANTHROPIC_BASE_URL: "http://<ollama-host>:11434"
    api_base: "http://<ollama-host>:11434"

models:
  sonnet: anthropic
  opus: anthropic
  haiku: anthropic

scheduled_tasks:
  - cron: "0 7 * * *"
    voice: true
    prompt: "Run /7am"
  - cron: "0 13 * * *"
    voice: false
    prompt: "Run /1pm"
  - cron: "0 3 * * *"
    voice: false
    prompt: "Run /3am"
```

### Fields

| Field | Default | Description |
|---|---|---|
| `server_port` | `8420` | Gateway HTTP port |
| `idle_timeout_minutes` | `30` | Kill sessions idle longer than this |
| `max_sessions` | `10` | Maximum concurrent Claude subprocesses |
| `default_model` | `sonnet` | Model alias to use when none specified |
| `providers` | — | Provider configs (see [Providers](providers.md)) |
| `models` | — | Map of model alias → provider name |

## Secrets

All application secrets live in the repo's `.env` (mode 600, gitignored). Loaded into the process environment at startup by systemd's `EnvironmentFile=` (live service) or python-dotenv (stateless tools / tests). See [`specs/secret-management.md`](../../specs/secret-management.md) for the full spec.

### Reading Secrets

```python
import os
token = os.environ["TELEGRAM_BOT_TOKEN"]
```

Or via the thin `core/secrets.py::get_credential(key)` wrapper, retained so a future switch to a real secret store is a one-place change.

### Common keys

| Key | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | Claude CLI subprocesses |
| `TELEGRAM_BOT_TOKEN` | Telegram client |
| `COMMS_BEARER_TOKEN`, `COMMS_ADMIN_BEARER_TOKEN` | Comms gateway auth |
| `LUCENT_BEARER_TOKEN` | Lucent KG + vector store auth |
| `HIVE_TOOLS_TOKEN` | hive-tools API auth |
| `PLANKA_EMAIL`, `PLANKA_PASSWORD`, `PLANKA_URL` | Kanban board |

## Environment Variables (Per-Container)

Each container receives only the env vars it needs. Set in `docker-compose.yml`:

| Var | Containers | Purpose |
|---|---|---|
| `SESSIONS_DB_PATH` | server | SQLite database path |
| `HIVE_MIND_SERVER_URL` | bots | Gateway URL |
| `VOICE_SERVER_URL` | bots | Voice server URL |
| `WHISPER_MODEL` | voice-server | Whisper model size |
| `TTS_BACKEND` | voice-server | TTS engine: `chatterbox` (default) or `bark` |
| `VOICE_REF_DIR` | voice-server | Directory containing per-mind `{voice_id}.wav` reference clips |
