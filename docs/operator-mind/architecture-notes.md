# Operator mind — architecture notes

Background detail and known sharp edges that the
[`operator-mind.md`](../operator-mind.md) install pattern references but
doesn't explain in depth. Read this when something in the install pattern
seems arbitrary, or when an install hits one of the listed gotchas.

## 1. `CLAUDE_CONFIG_DIR` and the import-time mkdir

`mind_server.py` reads `CLAUDE_CONFIG_DIR` at module-import time:

```python
_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/home/hivemind/.claude-config"))
```

`_setup_config_dir()` runs at import and calls
`_CONFIG_DIR.mkdir(parents=True, exist_ok=True)`. The default
(`/home/hivemind/.claude-config`) is the Docker container's home directory
and crashes with `PermissionError: [Errno 13] Permission denied: '/home/hivemind'`
on bare-metal because `/home/hivemind` doesn't exist there.

The recipe sets `CLAUDE_CONFIG_DIR=/home/<run_user>/.claude` explicitly so:

- `mkdir(exist_ok=True)` is a no-op (directory already exists).
- The host's existing OAuth tokens at `~/.claude/.credentials.json` are used.
- The harmless `host-creds-source not found at /home/hivemind/.host-claude/.credentials.json`
  warning is logged and ignored.

**Long-term fix candidate.** Detect bare-metal deployment in `mind_server.py`
(`Path("/home/hivemind").exists()` is `False`) and fall back to
`Path.home() / ".claude"` when no env override is set.

## 2. `HIVE_MIND_SERVER_URL` — legacy single-purpose env var

The gateway URL and the secrets-API URL are read from different env vars:

- `bots/telegram_bot.py` reads `COMMS_URL` (and `COMMS_BEARER_TOKEN`) for
  the comms gateway.
- `mind_server.py` reads `HIVE_MIND_SERVER_URL` to call the secrets API
  (`/secrets/scopes/<name>`, `/secrets/<key>`).

In the operator-mind setup, point `HIVE_MIND_SERVER_URL` at the same host:port as
`COMMS_URL` (hive-comms exposes the secrets API). The mind_server call is
wrapped in `try/except` and soft-fails to "no secrets" if the endpoint isn't
present, so the worst case is a startup log line, not a crash.

**Long-term fix candidate.** Rename `HIVE_MIND_SERVER_URL` in `mind_server.py`
to `COMMS_URL` (since that's where the secrets API actually lives) and delete
the legacy alias.

## 3. The `gateway_url` field is a historical artifact

`minds/<name>/MIND.md` has a `gateway_url` frontmatter field whose name
predates the current split between gateway and mind backend. The field has
only one consumer: anything inside this repo that reads MIND.md to find where
the mind's HTTP backend lives. That target is the mind_server. So
`gateway_url: http://localhost:<MIND_SERVER_PORT>` is unambiguous.

Federation — peers reaching this mind — goes through hive-comms broker
registration, not this frontmatter field. The peers learn this mind's address
from their own broker's registration, which points at hive-comms's
`COMMS_URL`.

**Long-term fix candidate.** Rename the frontmatter field to `mind_server_url`
to drop the misleading name. Touches every MIND.md and any reader.

## 4. Telegram bot error reporting

The bot's per-handler `try/except` blocks reply with the actual exception
text (truncated to ~3.5k chars) instead of a generic
`"Something went wrong. Try again or use /clear."`:

```python
except Exception:
    log.exception("Error processing message in chat %s", chat_id)
    err = f"⚠ {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
    await update.message.reply_text(err)
```

This is in all four handlers (text, queued-batch, photo, voice). The journal
still gets the full traceback via `log.exception`.

For an operator mind where Telegram is the primary debugging surface, surfacing
real errors in chat saves a `journalctl` round-trip on most failure modes.
Cost: a Telegram user sees a Python `TypeError` rather than a polite "try
again." For an operator mind this is the right trade — there's exactly one
Telegram user, and it's the operator.

When an upstream service returns a partial SSE stream after an error, the
client sees `aiohttp.ClientPayloadError: Response payload is not completed`
in chat. The root cause is in the journal under the same timestamp. A future
improvement would be a typed error event in the SSE stream itself.

## 5. python-telegram-bot embedded via `asyncio.gather`

`bots/telegram_bot.py:run_telegram_bot()` is an async coroutine, not the
library's standard `app.run_polling()` blocking call. This is required for
the bot to share an event loop with `mind_server` in the same process.

The non-obvious bit: `ApplicationBuilder().post_init(_on_startup)` and
`.post_shutdown(_on_shutdown)` hooks **do not fire** under manual
`app.initialize()`. They only run inside `run_polling`/`run_webhook`.

Symptom if forgotten: bot starts, accepts messages, but the module-global
`gateway` (set in `_on_startup`) is `None`, producing
`AttributeError: 'NoneType' object has no attribute 'query_stream'`.

The fix `bots/telegram_bot.py` already applies: call `_on_startup(app)` and
`_on_shutdown(app)` manually inside the coroutine:

```python
async def run_telegram_bot() -> None:
    token = _get_bot_token()
    app = _build_application(token)
    await app.initialize()
    await _on_startup(app)         # post_init won't fire — call manually
    await app.start()
    await app.updater.start_polling()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await _on_shutdown(app)    # post_shutdown won't fire — call manually
        await app.shutdown()
```

When you add a new surface (Discord, etc.) that uses a library with similar
lifecycle hooks, check whether those hooks fire under manual init before
relying on them.

## 6. Voice reference file path — code vs doc mismatch (upstream)

An operator mind's live voice service is typically the upstream
`hive-mind-voice` container, not this repo. The doc-vs-code mismatch lives
there:

The voice identity doc in the upstream `hive-mind` repo says
reference clips live at `voice_ref/{voice_id}.wav`. The actual code
(`voice/voice_server.py`) reads
`<voice_install>/minds/<voice_id>/voice_ref.wav`. **Code is the source of
truth.**

For an operator mind using the existing voice service, the per-mind
reference clip goes into the **voice service's install**, not this mind's:

```
<existing_voice_install>/minds/<operator_mind_name>/voice_ref.wav
```

If the voice service runs inside a Docker container with `minds/` bind-mounted
from a host clone, that path resolves to e.g.
`<hive-mind-clone>/minds/<name>/voice_ref.wav`.

If the file is missing, `_resolve_voice_ref` returns `None` and Chatterbox
synthesises with no reference clip — the unconditioned default voice, which
may incidentally resemble another mind's voice and is confusing to debug.

**Long-term fix candidate.** Reconcile the upstream doc and have
`_resolve_voice_ref` log a warning when no clip is found.

## 7. Soul lives in the knowledge graph, not the soul file

`souls/<name>.md` is a seed file — it isn't read at runtime. At every dispatch,
hive-comms `bootstrap_loader.compose_prompt_blocks` queries lucent for a
`Mind` node:

- `type: Mind`
- `name: <Name>` (capitalised)
- `mind_id: <uuid>` (matches the `MIND_ID` env var)
- reads `properties.soul_values` (a list of strings)

The seed file's content is ingested into the graph **once during install**
via the `/seed-mind` skill (or a hand-rolled HTTP POST to
`LUCENT_URL_SELF/graph/upsert`). Every line of the file becomes one entry in
`soul_values`. Verbatim — no summarising, no collapsing.

hive-comms reads `soul_values` fresh on every spawn, so the running mind
sees soul edits immediately. No service restart needed after seeding or
updating.

When the running mind discovers something that meaningfully shapes its
identity, the `auto_remember.sh` Stop hook's soul self-reflect branch evaluates
whether to add to `soul_values` and writes via
`LUCENT_URL_SELF/graph/properties/merge` — additive, preserves unrelated keys.

**Why `properties/merge` not `properties` upsert.** `/graph/upsert` is a
full-replace on the `properties` blob — using it would clobber every other
property on the node. `/graph/properties/merge` is the additive form. See the
write-path warning in
[the main CLAUDE.md](../../CLAUDE.md#operational-rules).
