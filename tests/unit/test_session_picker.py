"""The Telegram session picker, at the layer each behaviour actually lands.

The picker is a pure function over the gateway's session list, so the
rendering requirements need no bot at all; the routing ones drive the real
handlers with a stand-in for Telegram's transport, because routing is the
behaviour and Telegram is only how the tap or the command arrives.

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
        monkeypatch.setattr(bot, "gateway", MagicMock(
            suspend_session=fake_suspend,
            find_active_session=AsyncMock(return_value=None),
        ))
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
# Requirement 4 — /suspend puts a conversation to sleep: the chat's own when
# given nothing, the named one when given an id.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_bare_suspend_suspends_the_conversation_this_chat_is_in(monkeypatch):
    """Breaks if the bare form stops resolving the chat's own binding, or
    starts demanding an id the operator would have to copy out of a picker."""
    suspended = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value="sess-here"),
        suspend_session=AsyncMock(
            side_effect=lambda sid: suspended.append(sid) or {"status": "suspended"}),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=[])
    await bot.cmd_suspend(update, ctx)

    assert suspended == ["sess-here"]


@pytest.mark.asyncio
async def test_suspend_with_an_id_suspends_that_one_not_the_current_one(monkeypatch):
    """The picker draws ids and a conversation held elsewhere has no other way
    to be reached. Breaks if the argument is ignored for the chat's binding."""
    suspended = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value="sess-here"),
        suspend_session=AsyncMock(
            side_effect=lambda sid: suspended.append(sid) or {"status": "suspended"}),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["33333333-cccc"])
    await bot.cmd_suspend(update, ctx)

    assert suspended == ["33333333-cccc"]


@pytest.mark.asyncio
async def test_a_bare_suspend_with_nothing_to_suspend_suspends_nothing(monkeypatch):
    """An unbound chat has no conversation to name. Breaks if the empty
    lookup is ever passed through to the gateway as a target."""
    suspended = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value=None),
        suspend_session=AsyncMock(
            side_effect=lambda sid: suspended.append(sid) or {"status": "suspended"}),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=[])
    await bot.cmd_suspend(update, ctx)

    assert suspended == []


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


# ===========================================================================
# The grill found four behaviours asserted nowhere: `/sessions` itself never
# ran, `/rename` itself never ran, the suspend call's URL was never seen, and
# the callback was never proved answered. Each mutation below was survived by
# the original suite.
# ===========================================================================
class _Recorder:
    """Records what the bot sent, standing in for Telegram only."""

    def __init__(self):
        self.text = None
        self.markup = None

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.text = text
        self.markup = reply_markup


def _chat_update(recorder, args=None):
    update = MagicMock()
    update.message = recorder
    update.effective_user.id = 4242
    update.effective_chat.id = 99
    ctx = MagicMock()
    ctx.args = args or []
    return update, ctx


# --- Requirement 1, at the command rather than the renderer ----------------
@pytest.mark.asyncio
async def test_the_sessions_command_sends_a_keyboard_not_a_list(monkeypatch):
    """Breaks if `/sessions` stops attaching the markup, which the renderer's
    own tests cannot see: they never run the command."""
    sessions = [
        {"id": "11111111-aaaa", "summary": "Taxes", "status": "running", "last_active": 0},
        {"id": "22222222-bbbb", "summary": "Roof", "status": "suspended", "last_active": 0},
    ]
    recorder = _Recorder()
    update, ctx = _chat_update(recorder)
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(server_command=AsyncMock(return_value=sessions)))
    monkeypatch.setattr(bot.labels_client, "fetch_labels", AsyncMock(return_value={}))

    await bot.cmd_sessions(update, ctx)

    assert recorder.markup is not None, "/sessions sent no buttons at all"
    payloads = [b.callback_data for row in recorder.markup.inline_keyboard for b in row]
    assert "sw:11111111-aaaa" in payloads
    assert "sw:22222222-bbbb" in payloads


@pytest.mark.asyncio
async def test_a_long_session_list_is_capped_and_says_so(monkeypatch):
    """Telegram rejects an oversized keyboard and the error handler swallows
    it, so the operator sees nothing. Breaks if the cap is removed or the
    count stops being reported."""
    sessions = [
        {"id": f"{i:08d}-xxxx", "summary": str(i), "status": "running", "last_active": 0}
        for i in range(30)
    ]
    recorder = _Recorder()
    update, ctx = _chat_update(recorder)
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(server_command=AsyncMock(return_value=sessions)))
    monkeypatch.setattr(bot.labels_client, "fetch_labels", AsyncMock(return_value={}))

    await bot.cmd_sessions(update, ctx)

    switch_rows = [
        b for row in recorder.markup.inline_keyboard for b in row
        if b.callback_data.startswith("sw:")
    ]
    assert len(switch_rows) == 12
    assert "12 of 30" in recorder.text


# --- Requirement 3, against the payload the button really carries ----------
def test_the_new_button_carries_a_payload_with_no_target_in_it():
    """The original assertion decoded a module constant, which proved nothing
    about the button. Breaks if `encode` ever emits a separator for it."""
    rows = picker.build_session_rows(
        [{"id": "11111111-aaaa", "summary": "x", "status": "running"}], {}
    )
    new_payload = rows[-1][0].callback_data

    assert picker.decode(new_payload) == (picker.CB_NEW, "")


# --- Requirement 4, at the HTTP call rather than the argument boundary -----
@pytest.mark.asyncio
async def test_suspend_posts_to_the_url_of_the_session_it_was_given():
    """Breaks if the client ever posts to a fixed id — which the handler test
    cannot see, because it replaces this method entirely."""
    from bots.gateway_client import GatewayClient

    posted = []

    class _Resp:
        status = 200

        async def json(self):
            return {"status": "suspended"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Http:
        def post(self, url, **kwargs):
            posted.append(url)
            return _Resp()

    client = GatewayClient(
        _Http(), "http://gw.test", "telegram", mind_id="m", bearer_token="t"
    )

    await client.suspend_session("33333333-cccc")

    assert posted == ["http://gw.test/sessions/33333333-cccc/suspend"]


# --- Requirement 5, at the command rather than the body builder ------------
@pytest.mark.asyncio
async def test_a_bare_rename_command_writes_nothing_at_all(monkeypatch):
    """`rename_body` returning None only helps if the handler obeys it.
    Breaks if `cmd_rename` ever PUTs on an empty name."""
    writes = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot.labels_client, "configured", lambda: True)
    monkeypatch.setattr(bot.labels_client, "fetch_labels",
                        AsyncMock(return_value={"sess-1": {"name": "Roof", "color": "#3481cc"}}))
    monkeypatch.setattr(bot.labels_client, "put_label",
                        AsyncMock(side_effect=lambda sid, body: writes.append((sid, body)) or True))
    monkeypatch.setattr(bot, "gateway",
                        MagicMock(find_active_session=AsyncMock(return_value="sess-1")))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=[])
    await bot.cmd_rename(update, ctx)
    assert writes == [], "a bare /rename reached the label store"

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["New", "roof"])
    await bot.cmd_rename(update, ctx)
    assert writes == [("sess-1", {"name": "New roof", "color": "#3481cc"})]


@pytest.mark.asyncio
async def test_a_rename_refuses_when_the_current_label_cannot_be_read(monkeypatch):
    """An unreadable store used to look like an empty one, so the write went
    out with a blank colour and erased the one set at the tile. Breaks if a
    failed read is ever treated as "no labels"."""
    writes = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot.labels_client, "configured", lambda: True)
    monkeypatch.setattr(bot.labels_client, "fetch_labels", AsyncMock(return_value=None))
    monkeypatch.setattr(bot.labels_client, "put_label",
                        AsyncMock(side_effect=lambda sid, body: writes.append((sid, body)) or True))
    monkeypatch.setattr(bot, "gateway",
                        MagicMock(find_active_session=AsyncMock(return_value="sess-1")))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["New", "roof"])
    await bot.cmd_rename(update, ctx)

    assert writes == []


# --- The spinner, and the gate on it ---------------------------------------
@pytest.mark.asyncio
async def test_every_tap_answers_the_callback(tapped):
    """An unanswered callback spins on the phone forever with no error
    anywhere. Breaks if any path returns before answering."""
    for payload in (picker.CB_NEW, picker.encode(picker.CB_SWITCH, "a"), "nonsense"):
        query, _c, _s, update = tapped(payload)
        await bot.on_session_button(update, None)
        assert query.answer.await_count == 1, f"{payload} left the button spinning"


@pytest.mark.asyncio
async def test_a_tap_from_anyone_but_the_owner_does_nothing(tapped, monkeypatch):
    """Every typed command goes through `_auth_check`; a tap has its own gate.
    Breaks if that gate is dropped, which the other handler tests cannot see
    because they all stub it to True."""
    query, commands, suspended, update = tapped(picker.encode(picker.CB_SWITCH, "a"))
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: False)

    await bot.on_session_button(update, None)

    assert commands == []
    assert suspended == []


# ===========================================================================
# From the edge review: three failures that are invisible from where the
# operator stands.
# ===========================================================================
@pytest.mark.asyncio
async def test_rename_never_creates_the_conversation_it_is_naming(monkeypatch):
    """`ensure_session` creates when it finds nothing — minting a conversation,
    binding the chat to it and spawning a harness. A rename against a suspended
    conversation therefore started an empty one, named that, and said it worked.

    Breaks if the lookup ever creates again.
    """
    created = []
    writes = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot.labels_client, "configured", lambda: True)
    monkeypatch.setattr(bot.labels_client, "fetch_labels", AsyncMock(return_value={}))
    monkeypatch.setattr(bot.labels_client, "put_label",
                        AsyncMock(side_effect=lambda sid, body: writes.append(sid) or True))
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value=None),
        ensure_session=AsyncMock(side_effect=lambda u, c: created.append(c) or "brand-new"),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["weekend", "notes"])
    await bot.cmd_rename(update, ctx)

    assert created == [], "/rename started a conversation"
    assert writes == [], "/rename labelled a conversation it had just created"


@pytest.mark.asyncio
async def test_a_rejected_suspend_body_is_reported_as_an_error(monkeypatch):
    """comms raises through its own handlers as {"error": ...}, but a body
    FastAPI rejects comes back as {"detail": ...}. Reading only the first
    reported a 422 as a successful suspend.

    Breaks if the handler goes back to checking one key.
    """
    async def fastapi_rejection(session_id):
        return {"detail": [{"loc": ["path", "session_id"], "msg": "value is not valid"}]}

    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value="sess-here"),
        suspend_session=fastapi_rejection,
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["33333333-cccc"])
    await bot.cmd_suspend(update, ctx)

    assert recorder.text.startswith("Error:"), \
        f"a rejected suspend reported as {recorder.text!r}"


def test_one_unsendable_row_does_not_take_the_whole_picker_with_it():
    """Telegram rejects an oversized callback_data on the entire sendMessage,
    so one bad row means no buttons at all and `/sessions` replies with
    nothing. Breaks if the per-row limit is dropped.
    """
    sessions = [
        {"id": "11111111-aaaa", "summary": "fine", "status": "running"},
        {"id": "x" * 200, "summary": "far too long", "status": "running"},
        {"id": "22222222-bbbb", "summary": "also fine", "status": "running"},
    ]

    payloads = [
        b.callback_data for row in picker.build_session_rows(sessions, {}) for b in row
    ]

    assert "sw:11111111-aaaa" in payloads
    assert "sw:22222222-bbbb" in payloads
    assert all(
        len(p.encode("utf-8")) <= picker.CALLBACK_DATA_LIMIT for p in payloads
    )
