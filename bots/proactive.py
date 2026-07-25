"""Proactive delivery queue shared by ``mind_server`` and the Telegram bot.

Both run in the same process/event loop (see
``launch_mind_server_and_bots.py``), so unsolicited assistant output produced
by the harness subprocess when no inbound request is draining stdout can be
handed to the bot in-process via this module-level queue.

Kept intentionally tiny and free of imports from either side so it can be
imported by ``mind_server`` and ``bots.telegram_bot`` without a circular
dependency.
"""

import asyncio

# (chat_id, text) items awaiting delivery to Telegram.
_queue: "asyncio.Queue[tuple[int, str]]" = asyncio.Queue()


def enqueue(chat_id: int, text: str) -> None:
    """Enqueue an unsolicited assistant text turn for delivery to ``chat_id``."""
    _queue.put_nowait((chat_id, text))


async def get() -> "tuple[int, str]":
    """Await the next ``(chat_id, text)`` item to deliver."""
    return await _queue.get()


def _reset() -> None:
    """Test helper — drain any pending items so tests start from empty."""
    global _queue
    _queue = asyncio.Queue()
