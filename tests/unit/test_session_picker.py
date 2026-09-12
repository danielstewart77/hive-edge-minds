"""The Telegram session picker, at the layer each behaviour actually lands.

Four requirements, four tests. The picker is a pure function over the
gateway's session list, so the first and third need no bot at all; the second
and fourth drive the real callback handler with a stand-in for Telegram's
transport, because routing a tap is the behaviour and Telegram is only how the
tap arrives.

What is deliberately *not* here: a test that a tap on a conversation the
gateway no longer holds reports it as gone. The good id would come from the
session list and the bad one would be derived as an id absent from that same
list — but membership in that list is the thing under test, so it would only
prove the list agrees with itself. The behaviour is implemented; it is not
testable without circularity, and asserting the error string is a
spellchecker.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-never-valid")

from bots import session_picker as picker  # noqa: E402
import bots.telegram_bot as bot  # noqa: E402


# ---------------------------------------------------------------------------
# Requirement 1 — /sessions gives one button per open conversation, in the
# order the gateway returned them, each carrying that conversation's own id.
# ---------------------------------------------------------------------------
def test_one_switch_button_per_conversation_in_gateway_order():
    """Breaks if anyone re-sorts, collapses, or drops the id from the payload."""
    sessions = [
        {"id": "11111111-aaaa", "summary": "Taxes"},
        {"id": "22222222-bbbb", "summary": "Roof quote"},
        {"id": "33333333-cccc", "summary": "Sloan's laptop"},
    ]

    rows = picker.build_session_rows(sessions, {})

    switch_payloads = [
        row[0].callback_data for row in rows
        if row[0].callback_data.startswith(f"{picker.CB_SWITCH}{picker.CB_SEP}")
    ]
    assert switch_payloads == [
        "sw:11111111-aaaa", "sw:22222222-bbbb", "sw:33333333-cccc",
    ]
    # The operator's label beats the gateway's summary on the face of the
    # button; an unlabelled conversation keeps the summary.
    labelled = picker.build_session_rows(
        sessions, {"22222222-bbbb": {"name": "New roof", "color": "#3481cc"}}
    )
    assert "New roof" in labelled[1][0].text
    assert "Roof quote" not in labelled[1][0].text
    assert "Taxes" in labelled[0][0].text


def test_an_empty_list_still_offers_a_new_session():
    """Breaks if the new-session button is only drawn alongside existing rows."""
    rows = picker.build_session_rows([], {})
    assert [b.callback_data for row in rows for b in row] == [picker.CB_NEW]


# ---------------------------------------------------------------------------
# Requirement 2 — the new-session button starts a conversation and its payload
# is never read as a session id.
# ---------------------------------------------------------------------------
@pytest.fixture()
def tapped(monkeypatch):
    """Drive the real handler; Telegram's transport is the only thing faked."""
    def _tap(payload: str):
        query = MagicMock()
        query.data = payload
        query.answer = AsyncMock()
        query.message.reply_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = 4242
        update.effective_chat.id = 99

        commands: list[str] = []
        suspended: list[str] = []

        async def fake_command(content, user_id, chat_id):
            commands.append(content)
            return "ok"

        async def fake_suspend(session_id):
            suspended.append(session_id)
            return {"status": "suspended"}

        # `gateway` is built in main(), so it is None at import time. The HTTP
        # client is the only thing standing in here; the routing under test is
        # the real handler's.
        monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
        monkeypatch.setattr(bot, "_handle_server_command", fake_command)
        monkeypatch.setattr(bot, "gateway", MagicMock(suspend_session=fake_suspend))
        return query, commands, suspended, update
    return _tap


@pytest.mark.asyncio
async def test_the_new_button_starts_a_session_and_is_never_read_as_an_id(tapped):
    """Breaks if the sentinel ever parses as a target, or routes to switch."""
    query, commands, suspended, update = tapped(picker.CB_NEW)

    await bot.on_session_button(update, None)

    assert commands == ["/new"]
    assert suspended == []
    # The payload carries no separator, so nothing downstream can read a
    # target out of it.
    assert picker.decode(picker.CB_NEW) == (picker.CB_NEW, "")


@pytest.mark.asyncio
async def test_a_tapped_conversation_switches_to_that_id(tapped):
    """Breaks if the handler switches on anything but the id it was handed."""
    query, commands, suspended, update = tapped(
        picker.encode(picker.CB_SWITCH, "22222222-bbbb")
    )

    await bot.on_session_button(update, None)

    assert commands == ["/switch 22222222-bbbb"]


# ---------------------------------------------------------------------------
# Requirement 3 — a bare rename leaves an existing label alone.
# ---------------------------------------------------------------------------
def test_a_bare_rename_sends_nothing_rather_than_erasing_the_label():
    """The terminal deletes the row when name and colour are both blank.

    Breaks the moment an empty name is passed through as a body instead of
    being refused here.
    """
    existing = {"name": "New roof", "color": "#3481cc"}

    assert picker.rename_body("", existing) is None
    assert picker.rename_body("   ", existing) is None

    assert picker.rename_body("Roof, round two", existing) == {
        "name": "Roof, round two",
        "color": "#3481cc",
    }


# ---------------------------------------------------------------------------
# Requirement 4 — the suspend button suspends that conversation.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_suspend_button_suspends_the_tapped_conversation(tapped):
    """Breaks if suspend is routed as a server command or loses the id."""
    query, commands, suspended, update = tapped(
        picker.encode(picker.CB_SUSPEND, "33333333-cccc")
    )

    await bot.on_session_button(update, None)

    assert suspended == ["33333333-cccc"]
    assert commands == []


# ---------------------------------------------------------------------------
# Carried forward from tests/unit/test_telegram_session_picker.py, which tested
# the numbered text list this replaced. The rendering moved from
# `_format_sessions` to `button_text`; the behaviour did not, so the test moved
# with it rather than being deleted.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "session, expected",
    [
        ({"id": "a", "summary": "mine", "adoptable": False, "surface": "telegram"}, ""),
        ({"id": "a", "summary": "mine", "adoptable": True, "surface": "terminal"},
         "— on terminal"),
        ({"id": "a", "summary": "mine", "adoptable": True}, "— on another surface"),
    ],
    ids=["own", "held-elsewhere", "held-elsewhere-unnamed"],
)
def test_a_conversation_held_elsewhere_says_so_on_its_button(session, expected):
    """Tapping an adoptable row *moves* the conversation, ending it at the
    other end. A button that looks like every other button hides that.

    Breaks if the marker is dropped, or if it starts appearing on the
    operator's own conversations.
    """
    text = picker.button_text(session, {})
    if expected:
        assert expected in text
    else:
        assert "on " not in text
