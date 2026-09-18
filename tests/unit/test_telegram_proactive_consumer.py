"""Unit tests for the Telegram proactive-delivery consumer.

The consumer drains bots.proactive and posts each item via the Application's
bot, splitting messages over Telegram's 4096-char limit.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bots import proactive
from bots import telegram_bot


@pytest.fixture(autouse=True)
def _fresh_queue():
    proactive._reset()
    yield
    proactive._reset()


async def _run_consumer_until_drained(app):
    task = asyncio.create_task(telegram_bot._proactive_consumer(app))
    # Give the consumer a few loop turns to drain the queue.
    for _ in range(50):
        await asyncio.sleep(0.005)
        if proactive._queue.empty():
            break
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_consumer_posts_item_to_correct_chat():
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    proactive.enqueue(4242, "unsolicited turn")

    await _run_consumer_until_drained(app)

    app.bot.send_message.assert_awaited_once_with(chat_id=4242, text="unsolicited turn")


async def test_consumer_splits_long_messages():
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    long_text = "x" * (telegram_bot.TELEGRAM_MSG_LIMIT + 100)
    proactive.enqueue(7, long_text)

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 2
    sent = "".join(c.kwargs["text"] for c in app.bot.send_message.await_args_list)
    assert sent == long_text
    for call in app.bot.send_message.await_args_list:
        assert call.kwargs["chat_id"] == 7
        assert len(call.kwargs["text"]) <= telegram_bot.TELEGRAM_MSG_LIMIT


async def test_a_failed_send_is_retried_rather_than_dropped(monkeypatch):
    """Requirement 13: an answer arrives late rather than never.

    This queue is where an undeliverable button answer lands, so a single
    failed attempt followed by a shrug is the bug, not the behaviour: the
    operator acted, the action happened, and the sentence saying so is gone.
    The first send raises and the second succeeds — one message, two attempts.
    """
    monkeypatch.setattr(telegram_bot, "_PROACTIVE_BACKOFF_S", (0, 0, None))
    app = MagicMock()
    app.bot.send_message = AsyncMock(side_effect=[RuntimeError("boom"), None])
    proactive.enqueue(1, "will land on the retry")

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_count == 2
    assert app.bot.send_message.await_args.kwargs["text"] == "will land on the retry"


async def test_a_permanently_undeliverable_message_does_not_block_the_queue(monkeypatch):
    """Requirement 13: retrying is bounded, because the queue has to drain.

    A message with nowhere to go — the bot blocked, the chat deleted — retried
    forever is a consumer that never reaches the next item, so one dead message
    silences every answer behind it. It is given up on, and the one behind it
    goes out.
    """
    monkeypatch.setattr(telegram_bot, "_PROACTIVE_BACKOFF_S", (0, 0, None))
    app = MagicMock()
    app.bot.send_message = AsyncMock(
        side_effect=[RuntimeError("blocked"), RuntimeError("blocked"),
                     RuntimeError("blocked"), None]
    )
    proactive.enqueue(1, "never deliverable")
    proactive.enqueue(2, "behind it")

    await _run_consumer_until_drained(app)

    assert app.bot.send_message.await_args.kwargs["chat_id"] == 2
    assert app.bot.send_message.await_args.kwargs["text"] == "behind it"
