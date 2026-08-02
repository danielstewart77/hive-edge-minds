# Configuration

## config.yaml

Non-secret settings live in `config.yaml`. Copy `config.yaml.example` to get started.

```yaml
server_port: 8420
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
| `default_model` | `sonnet` | Model alias offered as the default when none is specified |
| `providers` | — | Provider configs (see [Providers](providers.md)) |
| `models` | — | Map of model alias → provider name |

`config.py` also parses `autopilot_guards`, but nothing reads the parsed
value — a leftover from the retired in-repo gateway, which owned session
lifetime before that moved to hive-comms. There is no idle timeout to
configure: a browser terminal is never reaped for inactivity, and ends only
when it is explicitly closed.

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
