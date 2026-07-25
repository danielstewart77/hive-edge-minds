"""Per-process bot helpers — chat-id serialization and UI formatting.

In-process ``asyncio`` primitives keyed by chat-id, so two incoming
messages from the same chat can't both kick off Claude in parallel.
Lives in the bot's Python process; not shared via NS.
"""

import asyncio
from datetime import datetime

_locks: dict[int, asyncio.Lock] = {}
_chat_queues: dict[int, asyncio.Queue] = {}


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


def get_queue(chat_id: int) -> asyncio.Queue:
    if chat_id not in _chat_queues:
        _chat_queues[chat_id] = asyncio.Queue()
    return _chat_queues[chat_id]


def time_ago(ts: float) -> str:
    """Render a unix timestamp as a relative string ("5 min ago"). UI helper."""
    delta = datetime.now().timestamp() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)} min ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"
