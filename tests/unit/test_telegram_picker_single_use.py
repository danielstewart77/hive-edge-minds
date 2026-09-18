"""A picker is used once, and a tap is never silent.

Requirements 8 through 15.

On 2026-09-17 two `New session` taps arrived 75 seconds apart — queued at
Telegram's end while polling was down, then acted on back to back. The first
ended the conversation the chat held and created one; the second ended *that*
one and created another. Neither reply had arrived to say the first had
worked, so tapping again was the only reasonable thing to do.

Two defences, tested here. The picker stops working after one tap, so a stale
tap cannot act at all; and every tap produces a sentence, retried and then
queued rather than dropped.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_callback(data: str = "new", message_id: int = 99, chat_id: int = 456):
    """A tapped inline button."""
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.message_id = message_id
    update.callback_query.message.text = "Your conversations:"
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


@pytest.fixture(autouse=True)
def _patch_config():
    with patch("bots.telegram_bot.config") as mock_config:
        mock_config.telegram_allowed_users = {123}
        yield mock_config


@pytest.fixture(autouse=True)
def _clean_pickers():
    from bots import bot_utils

    bot_utils._reset_pickers()
    yield
    bot_utils._reset_pickers()


class TestPickerClaim:
    def test_a_picker_can_be_claimed_once(self) -> None:
        """Requirement 8: the second tap on one picker does not get to act."""
        from bots.bot_utils import claim_picker

        assert claim_picker(456, 99) is True
        assert claim_picker(456, 99) is False

    def test_a_fresh_picker_claims_independently(self) -> None:
        """Requirement 9: sending /sessions again gives a working list."""
        from bots.bot_utils import claim_picker

        assert claim_picker(456, 99) is True
        assert claim_picker(456, 100) is True
        assert claim_picker(456, 99) is False

    def test_pickers_in_different_chats_do_not_collide(self) -> None:
        """Requirement 8: a claim is about one message, not one message number."""
        from bots.bot_utils import claim_picker

        assert claim_picker(456, 99) is True
        assert claim_picker(789, 99) is True


@pytest.mark.asyncio
class TestTapBehaviour:
    async def test_tap_removes_the_keyboard(self) -> None:
        """Requirement 8: the buttons go away once one of them is used."""
        from bots.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("bots.telegram_bot._run_session_button", AsyncMock(return_value="New session: abcd1234")):
            await on_session_button(update, context)

        update.callback_query.edit_message_reply_markup.assert_awaited_once()
        assert update.callback_query.edit_message_reply_markup.call_args.kwargs[
            "reply_markup"
        ] is None

    async def test_second_tap_never_reaches_the_gateway(self) -> None:
        """Requirement 12: a spent picker refuses out loud and does nothing.

        This is the test that stands for the conversation lost on 2026-09-17.
        The assertion that matters is not the wording — it is that the action
        did not run.
        """
        from bots.telegram_bot import on_session_button, SPENT_PICKER_TEXT

        run = AsyncMock(return_value="New session: abcd1234")
        with patch("bots.telegram_bot._run_session_button", run):
            update, context = _make_callback()
            await on_session_button(update, context)
            again, context2 = _make_callback()  # same chat, same message id
            await on_session_button(again, context2)

        assert run.await_count == 1
        context2.bot.send_message.assert_awaited()
        assert SPENT_PICKER_TEXT in context2.bot.send_message.call_args.kwargs["text"]

    async def test_tap_names_the_outcome_on_the_picker(self) -> None:
        """Requirement 10: scrollback says what the tap chose."""
        from bots.telegram_bot import on_session_button

        update, context = _make_callback(data="sw:sess-1")
        with patch("bots.telegram_bot._run_session_button", AsyncMock(return_value='Resumed "Dragoman"')):
            await on_session_button(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        assert "Dragoman" in update.callback_query.edit_message_text.call_args[0][0]

    async def test_successful_tap_delivers_the_outcome(self) -> None:
        """Requirement 11: a tap that worked says what it did."""
        from bots.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("bots.telegram_bot._run_session_button", AsyncMock(return_value="New session: abcd1234")):
            await on_session_button(update, context)

        context.bot.send_message.assert_awaited()
        assert "abcd1234" in context.bot.send_message.call_args.kwargs["text"]

    async def test_failing_tap_still_delivers_a_sentence(self) -> None:
        """Requirement 11: there is no tap that produces silence."""
        from bots.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("bots.telegram_bot._run_session_button", AsyncMock(side_effect=RuntimeError("gateway down"))):
            await on_session_button(update, context)

        context.bot.send_message.assert_awaited()
        assert context.bot.send_message.call_args.kwargs["text"].strip()

    async def test_tap_is_acknowledged_before_the_work_runs(self) -> None:
        """Requirement 14: the tap is visibly received even if the answer is not.

        Order matters and is the whole requirement: acknowledging after the
        work means a slow or hanging action leaves the button spinning with
        nothing on screen, which is the failure being fixed.
        """
        from bots.telegram_bot import on_session_button

        order: list[str] = []
        update, context = _make_callback()
        update.callback_query.answer = AsyncMock(side_effect=lambda *a, **k: order.append("ack"))

        async def _work(*_a, **_k):
            order.append("work")
            return "New session: abcd1234"

        with patch("bots.telegram_bot._run_session_button", _work):
            await on_session_button(update, context)

        assert order == ["ack", "work"]


@pytest.mark.asyncio
class TestDeliveryIsNeverDropped:
    async def test_undeliverable_text_lands_on_the_proactive_queue(self) -> None:
        """Requirement 13: an answer arrives late rather than never."""
        from bots import proactive
        from bots.telegram_bot import _deliver

        proactive._reset()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("Bad Gateway"))

        with patch("bots.telegram_bot._DELIVER_BACKOFF_S", 0):
            delivered = await _deliver(bot, 456, 'Resumed "Dragoman"')

        assert delivered is False
        chat_id, text = await proactive.get()
        assert chat_id == 456
        assert text == 'Resumed "Dragoman"'

    async def test_delivery_retries_before_giving_up(self) -> None:
        """Requirement 13: a blip costs a retry, not the message."""
        from bots.telegram_bot import _deliver

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[RuntimeError("Bad Gateway"), None])

        with patch("bots.telegram_bot._DELIVER_BACKOFF_S", 0):
            delivered = await _deliver(bot, 456, "ok")

        assert delivered is True
        assert bot.send_message.await_count == 2

    async def test_delivery_failure_is_a_warning(self, caplog) -> None:
        """Requirement 15: not swallowed at INFO, where nobody reads it."""
        import logging

        from bots import proactive
        from bots.telegram_bot import _deliver

        proactive._reset()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("Bad Gateway"))

        with patch("bots.telegram_bot._DELIVER_BACKOFF_S", 0), \
                caplog.at_level(logging.WARNING):
            await _deliver(bot, 456, 'Resumed "Dragoman"')

        assert any(r.levelno >= logging.WARNING for r in caplog.records)
