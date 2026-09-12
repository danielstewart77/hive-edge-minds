"""The operator's conversation labels, read and written over HTTP.

Labels live with the browser terminal, not with hive-comms: the operator names
a conversation at the tile, and the name is theirs, not the gateway's. The
terminal exposes them behind `TERMINAL_LABELS_TOKEN`, a credential that
renames a conversation and does nothing else — deliberately not the comms
bearer, which opens every route on the mind including the one that types into
a live pane.

Every call here degrades rather than raises. A picker with no labels is still
a picker: it falls back to the gateway's own summaries, which is exactly what
the numbered list showed before any of this existed. An unreachable label
store must cost the names on the buttons and nothing else.
"""

from __future__ import annotations

import logging
import os

import aiohttp

from hive_logging import configure_logging, log_event

log = configure_logging("hive-mind-telegram")

TERMINAL_URL = os.environ.get("TERMINAL_URL", "").rstrip("/")
TERMINAL_LABELS_TOKEN = os.environ.get("TERMINAL_LABELS_TOKEN", "")

_TIMEOUT = aiohttp.ClientTimeout(total=5)


def configured() -> bool:
    """Whether this mind has been given a label store to talk to.

    Reported rather than assumed, so a rename can say "labels aren't wired up
    here" instead of failing in a way that reads as the rename being broken.
    """
    return bool(TERMINAL_URL and TERMINAL_LABELS_TOKEN)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TERMINAL_LABELS_TOKEN}"}


async def fetch_labels() -> dict | None:
    """Every label the terminal holds, keyed by session id.

    ``None`` means the store could not be read; ``{}`` means it was read and
    holds nothing. A caller drawing buttons can treat both the same — it falls
    back to the gateway's summaries either way — but a caller about to *write*
    a label cannot. A failed read that looks like an empty store turns a
    rename into a write of `{"name": ..., "color": ""}`, which erases the
    colour set at the tile. The distinction is the whole point of the return.
    """
    if not configured():
        return None
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.get(
                f"{TERMINAL_URL}/api/terminal/labels",
                headers={**_headers(), "Cache-Control": "no-store"},
            ) as resp:
                if resp.status >= 400:
                    log_event(
                        log, "labels.fetch.failed", level=logging.WARNING,
                        surface="telegram", status_code=resp.status,
                    )
                    return None
                data = await resp.json()
                return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - a missing label is never fatal
        log_event(
            log, "labels.fetch.failed", level=logging.WARNING,
            surface="telegram", error=str(exc),
        )
        return None


async def put_label(session_id: str, body: dict) -> bool:
    """Write one conversation's label. True when the terminal accepted it."""
    if not configured():
        return False
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.put(
                f"{TERMINAL_URL}/api/terminal/labels/{session_id}",
                json=body,
                headers=_headers(),
            ) as resp:
                ok = resp.status < 400
                log_event(
                    log, "labels.write.completed" if ok else "labels.write.failed",
                    level=logging.INFO if ok else logging.WARNING,
                    surface="telegram", session_id=session_id, status_code=resp.status,
                )
                return ok
    except Exception as exc:  # noqa: BLE001
        log_event(
            log, "labels.write.failed", level=logging.WARNING,
            surface="telegram", session_id=session_id, error=str(exc),
        )
        return False
