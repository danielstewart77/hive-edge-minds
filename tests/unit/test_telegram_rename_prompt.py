"""Renaming by reply: tap the command, type the name.

Requirements 2 through 7. Tapping a command in Telegram's menu *sends* it, so
a `/rename` that answers "Usage: /rename <name>" is a menu entry that cannot
be used from the menu. It asks instead, and the reply carries the name.

The recognition is held nowhere — no pending-prompt registry, no expiry — so a
prompt sent before a restart is answerable after one. That is requirement 6,
and it is the reason `name_from_reply` takes message text rather than an id.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(
    text: str = "",
    reply_to_text: str | None = None,
    user_id: int = 123,
    chat_id: int = 456,
):
    """A Telegram update, optionally replying to a message carrying ``reply_to_text``."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.message.text = text
    update.message.reply_text = AsyncMock()
    if reply_to_text is None:
        update.message.reply_to_message = None
    else:
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.text = reply_to_text
    return update


@pytest.fixture(autouse=True)
def _patch_config():
    with patch("bots.telegram_bot.config") as mock_config:
        mock_config.telegram_allowed_users = {123}
        yield mock_config


@pytest.fixture()
def labels():
    """A configured, readable, writable label store."""
    with patch("bots.telegram_bot.labels_client") as lc:
        lc.configured = MagicMock(return_value=True)
        lc.fetch_labels = AsyncMock(return_value={"sess-1": {"color": "#5c913b"}})
        lc.put_label = AsyncMock(return_value=True)
        yield lc


@pytest.fixture()
def gateway():
    gw = AsyncMock()
    gw.find_active_session = AsyncMock(return_value="sess-1")
    with patch("bots.telegram_bot.gateway", gw):
        yield gw


class TestReplyRecognition:
    """The pure decision: is this message a name, or a turn?"""

    def test_reply_to_the_prompt_yields_the_name(self) -> None:
        """Requirement 3: replying to the prompt with a name renames."""
        from bots import rename_prompt

        # Sourced from live state: the prompt the module actually sends is the
        # prompt a reply is recognised against. A hardcoded copy here would
        # pass while the two had drifted apart.
        assert rename_prompt.name_from_reply(
            rename_prompt.PROMPT_TEXT, "Dragoman"
        ) == "Dragoman"

    def test_reply_to_anything_else_is_not_a_name(self) -> None:
        """Requirement 5: an ordinary reply is an ordinary message."""
        from bots import rename_prompt

        assert rename_prompt.name_from_reply(
            "Renamed to \"Dragoman\".", "what did that do?"
        ) is None

    def test_a_message_replying_to_nothing_is_not_a_name(self) -> None:
        """Requirement 5: the common case — a plain message is a turn."""
        from bots import rename_prompt

        assert rename_prompt.name_from_reply(None, "Dragoman") is None

    def test_a_blank_reply_names_nothing(self) -> None:
        """Requirement 3: an empty reply must not reach the label store.

        The terminal deletes the label row when name and colour are both blank,
        so a whitespace reply that got through would erase the name rather than
        set one.
        """
        from bots import rename_prompt

        assert rename_prompt.name_from_reply(rename_prompt.PROMPT_TEXT, "   ") is None

    def test_a_name_beginning_like_the_prompt_is_not_the_prompt(self) -> None:
        """Requirement 5: recognition is the whole message, not a prefix."""
        from bots import rename_prompt

        near = rename_prompt.PROMPT_TEXT[:20]
        assert rename_prompt.name_from_reply(near, "Dragoman") is None

    def test_recognition_survives_a_restart(self) -> None:
        """Requirement 6: the prompt never expires.

        Nothing in this process has ever sent a prompt — the module was just
        imported. A reply to one is still resolved, because the decision reads
        the replied-to text and consults no stored state. This fails the moment
        anyone introduces a pending-prompt registry keyed by message id, which
        is precisely the thing that would not survive `skippy.service`
        restarting between the question and the answer.
        """
        import importlib

        import bots.rename_prompt as fresh

        fresh = importlib.reload(fresh)
        assert fresh.name_from_reply(fresh.PROMPT_TEXT, "Dragoman") == "Dragoman"


@pytest.mark.asyncio
class TestBareRenameAsks:
    async def test_bare_rename_sends_a_force_reply_prompt(self, labels, gateway) -> None:
        """Requirement 2: `/rename` with no name asks for one."""
        from bots.telegram_bot import cmd_rename
        from bots import rename_prompt

        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_rename(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.call_args
        assert args[0] == rename_prompt.PROMPT_TEXT
        assert kwargs.get("reply_markup") is not None
        labels.put_label.assert_not_awaited()

    async def test_bare_rename_with_no_conversation_refuses_without_asking(
        self, labels, gateway
    ) -> None:
        """Requirement 7: don't ask for a name there is nothing to apply to."""
        from bots.telegram_bot import cmd_rename
        from bots import rename_prompt

        gateway.find_active_session = AsyncMock(return_value=None)
        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_rename(update, context)

        args, kwargs = update.message.reply_text.call_args
        assert args[0] != rename_prompt.PROMPT_TEXT
        assert kwargs.get("reply_markup") is None
        labels.put_label.assert_not_awaited()

    async def test_rename_with_a_name_writes_it_directly(self, labels, gateway) -> None:
        """Requirement 4: `/rename Dragoman` still renames in one go."""
        from bots.telegram_bot import cmd_rename
        from bots import rename_prompt

        update = _make_update()
        context = MagicMock()
        context.args = ["Dragoman"]

        await cmd_rename(update, context)

        labels.put_label.assert_awaited_once()
        session_id, body = labels.put_label.call_args[0]
        assert session_id == "sess-1"
        assert body["name"] == "Dragoman"
        # The colour set at the tile survives a rename.
        assert body["color"] == "#5c913b"
        assert update.message.reply_text.call_args[0][0] != rename_prompt.PROMPT_TEXT


@pytest.mark.asyncio
class TestReplyRenamesTheConversation:
    async def test_reply_to_prompt_writes_the_label(self, labels, gateway) -> None:
        """Requirement 3: the reply renames the conversation this chat is in."""
        from bots.telegram_bot import handle_text
        from bots import rename_prompt

        update = _make_update("Dragoman", reply_to_text=rename_prompt.PROMPT_TEXT)
        context = MagicMock()

        with patch("bots.telegram_bot._stream_to_message", AsyncMock()) as stream:
            await handle_text(update, context)

        labels.put_label.assert_awaited_once()
        assert labels.put_label.call_args[0][1]["name"] == "Dragoman"
        # Requirement 3: the name is a name, not a question for the mind.
        stream.assert_not_awaited()

    async def test_ordinary_message_reaches_the_harness(self, labels, gateway) -> None:
        """Requirement 5: dismissing the prompt and typing works as it always did."""
        from bots.telegram_bot import handle_text

        update = _make_update("what is the status of the deploy?")
        context = MagicMock()

        with patch("bots.telegram_bot._stream_to_message", AsyncMock(return_value=["ok"])) as stream:
            await handle_text(update, context)

        stream.assert_awaited()
        labels.put_label.assert_not_awaited()
