"""Command replies survive Telegram's 4096-character limit.

A command whose reply ran past the limit came back from Telegram as a
BadRequest, which the bot's error handler logs as a transient network blip
and drops. Nothing reached the chat, so the command read as broken rather
than as too chatty — /sessions spent months looking dead for exactly this
reason.
"""
import asyncio

from bots.telegram_bot import _chunk_message, _reply_chunked


class _Message:
    def __init__(self):
        self.sent: list[str] = []

    async def reply_text(self, text):
        assert len(text) <= 4096, "chunk handed to Telegram is over the limit"
        self.sent.append(text)


class _Update:
    def __init__(self):
        self.message = _Message()


def test_short_reply_is_one_message():
    update = _Update()
    asyncio.run(_reply_chunked(update, "No sessions found."))

    assert update.message.sent == ["No sessions found."]


def test_oversized_reply_is_split_and_fully_delivered():
    body = "".join(f"{i}. session line\n" for i in range(600))
    assert len(body) > 4096

    update = _Update()
    asyncio.run(_reply_chunked(update, body))

    assert len(update.message.sent) > 1
    assert "".join(update.message.sent) == body


def test_chunker_covers_the_exact_boundary():
    exact = "x" * 4096
    assert _chunk_message(exact) == [exact]
    assert _chunk_message(exact + "y") == [exact, "y"]
