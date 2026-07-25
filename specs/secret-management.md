# Secret Management

## Source of truth

All secrets live in the repo's `.env` (mode 600, gitignored). Loaded into
the process environment at startup by systemd's `EnvironmentFile=` (live
service) or python-dotenv (stateless tools / tests).

No keyring layer. A plaintext-file keyring backend has the same
plaintext-on-disk threat model as `.env` with extra indirection, so
`.env` is the whole store.

## Reading secrets

```python
import os
token = os.environ["TELEGRAM_BOT_TOKEN"]  # or os.getenv(...) for optional
```

`core/secrets.py::get_credential(key)` is a thin wrapper around
`os.getenv` retained so a future switch to a real secret store
(system keyring, vault, KMS) is a one-place change.

## Keys

Every key the mind needs is in `.env`. Naming convention: upper-case
underscore-separated. Examples (not exhaustive):

- `MIND_ID`, `MIND_NAME`
- `TELEGRAM_BOT_TOKEN`
- `COMMS_BEARER_TOKEN`, `COMMS_ADMIN_BEARER_TOKEN`
- `LUCENT_BEARER_TOKEN`, `LUCENT_URL_SELF`
- `HIVE_TOOLS_TOKEN`, `HIVE_TOOLS_URL`
- `PLANKA_EMAIL`, `PLANKA_PASSWORD`, `PLANKA_URL`
- `X_BEARER_TOKEN`, `COINGECKO_API_KEY` (stateless tools)

## Rules

- Never hardcode secrets in source.
- Never commit `.env` — it's gitignored. If a secret appears in a tracked
  file, treat it as compromised and rotate.
- New secrets get a new line in `.env` and an `os.getenv` (or
  `get_credential`) call at the read site. No new infrastructure
  required.

## Future migration

When real secret storage lands (system keyring, vault, KMS), swap
`core/secrets.py::get_credential` to call into it. Caller signature
stays the same.
