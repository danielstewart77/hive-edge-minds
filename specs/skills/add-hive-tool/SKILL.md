---
name: add-hive-tool
description: Add a new tool to the hive-tools API service. Use when extending the body (gmail / calendar / browser / linkedin / docker) with a new external integration. Covers router file, dependencies, Dockerfile, server wiring, HITL seed rows, container rebuild, and end-to-end verification.
argument-hint: [tool-name]
tools: Read, Write, Edit, Bash
user-invocable: true
---

# Add a tool to hive-tools

## Overview

hive-tools is the body service (its own repo, cloned at `<hive-tools-root>`), port 9421. It exposes an HTTP API that clients (containerised Hive Mind minds, bare-metal operator minds, future callers) reach with a bearer token. Each tool — gmail, calendar, browser, linkedin, docker — is a FastAPI router under `tools/` that gets included in `server.py`. HITL approval is enforced per route via the `hitl_gate(<tool_name>)` dependency.

This skill walks the full procedure for adding a new tool. **The non-obvious step is seeding `tool_hitl_settings` in the DB** — without it, the route works but the management UI at `/tools` won't show it.

## When to use

- Adding a new external integration (e.g., `slack`, `notion`, `stripe`)
- Splitting an existing tool into a new namespace
- Migrating an MCP tool into hive-tools

## Decision: which routes are HITL-gated?

Apply this matrix to every route you write:

| Route shape | HITL gate? | Why |
|---|---|---|
| **Write** — sends, posts, modifies external state | `Depends(hitl_gate("tool_name"))` | Default deny — the operator approves writes |
| **Read** that returns user-readable data (emails, events, docs) | `Depends(hitl_gate("tool_name"))` with `mode=off` in seed | Listed in management UI, no live prompts |
| **Read** that's pure metadata / status (health, list-of-things) | `Depends(require_api_token)` only | Auth-gated, no HITL, not in management UI |
| **Action that introduces external content** (web nav, search, fetch) | `Depends(hitl_gate(...))` mode=on | Prompt-injection surface — treat as write |

The `tool_hitl_settings` row uses **the exact string** passed to `hitl_gate(...)` — those have to match. Convention: `<service>_<verb>` (e.g., `gmail_send_email`, `browser_navigate`).

## Procedure

### Step 1 — Write the router

Create `<hive-tools-root>/tools/<service>.py` following the template:

```python
"""<Service> FastAPI router."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from auth_api import require_api_token
from hitl import hitl_gate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/<service>", tags=["<service>"])


class SomeWriteBody(BaseModel):
    field: str


@router.post("/action")
async def do_action(
    body: SomeWriteBody,
    caller: str = Depends(hitl_gate("<service>_action")),
) -> Any:
    # business logic, return a dict
    return {"status": "ok"}


@router.get("/info")
async def get_info(
    caller_mind: str = Depends(require_api_token),
) -> Any:
    return {"data": ...}
```

Match the conventions used by existing routers (`tools/gmail.py`, `tools/browser.py`).

### Step 2 — Update `requirements.txt`

Add any new pip dependencies. Keep the file alphabetically grouped where possible.

### Step 3 — Update `Dockerfile` (only if system-level deps are needed)

E.g., for Playwright we added Chromium and ran `playwright install --with-deps chromium`. Most tool additions don't need this.

### Step 4 — Wire the router in `server.py`

In `server.py` find the `# Include tool routers` block and append:

```python
    try:
        from tools.<service> import router as <service>_router

        app.include_router(<service>_router)
    except ImportError:
        pass
```

The `try/except ImportError` lets the server start even if the new tool's deps aren't installed — important for staged rollouts.

### Step 5 — Seed HITL settings in `db.py`

This is the step that's easy to miss. Open `<hive-tools-root>/db.py` and find `_DEFAULT_HITL_SETTINGS`. Add a section for your tool, listing **every** `tool_name` you used in `hitl_gate(...)`:

```python
    # <Service> writes — on
    ("<service>_action", "on", 15),
    # <Service> reads — off (listed in UI but not gated)
    ("<service>_info_listed", "off", 15),
```

Convention: `mode="on"` for writes/state-change/external-content. `mode="off"` for safe reads. Timeout `15` minutes is the standard sudo timeout (matches every existing entry).

**Routes that use `require_api_token` directly do NOT get a row** — they're not HITL-managed and the management UI deliberately doesn't list them.

### Step 6 — Build the container

```bash
cd <hive-tools-root>
docker compose build hive-tools
```

For tool additions that don't change Dockerfile, the build is fast (cached layers). Playwright-style additions take a few minutes.

### Step 7 — Restart the container

```bash
docker compose up -d hive-tools
```

This recreates the container with the new image, preserving the data volume (`./data`).

### Step 8 — Seed the live DB (only if the DB pre-existed)

`seed_default_hitl_settings` runs at startup with `INSERT OR IGNORE`, so a **fresh** DB picks up the new rows automatically. **An existing DB already has rows for older tools, and the OR IGNORE means new ones in the seed list ARE inserted on next startup** — so usually a restart is enough. However, if you need them seeded *immediately* without a restart (or the container is loaded with an old db.py), insert directly:

```bash
docker exec hive-tools python3 -c "
import sqlite3
conn = sqlite3.connect('/app/data/hivetools.db')
for tool_name in ['<service>_action_a', '<service>_action_b']:
    conn.execute(
        'INSERT OR IGNORE INTO tool_hitl_settings (tool_name, mode, sudo_timeout_minutes) VALUES (?, ?, ?)',
        (tool_name, 'on', 15)
    )
conn.commit()
print('seeded')
"
```

### Step 9 — Verify

Three checks:

```bash
# 1. Health
curl -sf http://localhost:9421/health

# 2. New routes appear in OpenAPI
curl -s http://localhost:9421/openapi.json | python3 -c "import sys,json; d=json.load(sys.stdin); print('\n'.join(p for p in d['paths'] if '/<service>' in p))"

# 3. Management UI rows present
docker exec hive-tools python3 -c "
import sqlite3
c = sqlite3.connect('/app/data/hivetools.db')
for r in c.execute(\"SELECT tool_name, mode FROM tool_hitl_settings WHERE tool_name LIKE '<service>_%'\"):
    print(r[0], r[1])
"
```

Then refresh `/tools` in the browser — the new entries should appear.

## Common pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| Forgot to seed `tool_hitl_settings` | Route works via curl, but management UI doesn't show it | Step 5 + step 8 |
| `tool_name` mismatch between `hitl_gate(...)` and seed entry | UI shows entry; tool calls 401 or 500 unexpectedly | Make the strings byte-identical |
| Forgot `try/except ImportError` around router include | Server fails to start when deps are missing | Match the existing wrapper pattern in `server.py` |
| Built image but didn't recreate container | New routes don't appear | `docker compose up -d hive-tools` (not just `restart`) |
| Edited `db.py` but the container has the old copy | Live DB doesn't get new entries via seed | Step 8 manual SQL injection |

## Notes

- hive-tools doesn't mount source code into the container — it's COPIED at build time. Always rebuild after editing.
- The `tools/<service>.py` filename should match what `server.py` imports (`from tools.<service>`).
- Pydantic v2 schemas — use `BaseModel` from `pydantic`. Optional fields with defaults; no need for `Optional[...]` wrappers.
- For HITL UX: `mode="on"` triggers a Telegram approval prompt before the request fires. `mode="off"` lists in the UI but bypasses the prompt. `mode="sudo"` (where supported) is "approve once for N minutes."
