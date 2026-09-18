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

# (chat_id, text, attempts) items awaiting delivery to Telegram.
#
# The attempt count rides on the item rather than living in the consumer,
# because a message that failed goes back to the *tail* of this queue rather
# than being retried in place. Retrying in place is simpler and wrong: the
# outage this queue exists to survive lasts hours, so holding one item for
# hours holds every answer behind it for hours too. Round-robin instead — each
# item gets its next attempt in turn, and a message with nowhere to go delays
# nothing.
_queue: "asyncio.Queue[tuple[int, str, int]]" = asyncio.Queue()


def enqueue(chat_id: int, text: str, attempts: int = 0) -> None:
    """Enqueue an assistant text turn for delivery to ``chat_id``.

    ``attempts`` is how many times delivery has already been tried, and is
    passed only by the consumer putting a failed item back.
    """
    _queue.put_nowait((chat_id, text, attempts))


async def get() -> "tuple[int, str, int]":
    """Await the next ``(chat_id, text, attempts)`` item to deliver."""
    return await _queue.get()


def drain() -> "list[tuple[int, str, int]]":
    """Take everything pending, leaving the queue empty.

    Used at shutdown. The queue is process memory, so a restart with items in
    it loses them — and losing them silently is the exact failure the delivery
    guarantee exists to close, reached through the one action the operator
    performs by hand.
    """
    items = []
    while True:
        try:
            items.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


def _reset() -> None:
    """Test helper — drain any pending items so tests start from empty."""
    global _queue
    _queue = asyncio.Queue()
