"""The Telegram session picker: conversations as buttons, not a numbered list.

A numbered list makes the reader do the gateway's job. You read "3.", you
type `/switch 3`, and the number means whatever the list meant at the moment
it was printed — which on a phone is frequently days ago and several
conversations back. The buttons carry the conversation's own id instead, so
a tap means the same thing whenever it happens.

Everything here is a pure function over the gateway's session list and the
terminal's label store. Nothing in this module talks to Telegram, reads the
network, or knows a chat exists; `telegram_bot` hands the rows it returns to
the send call and nothing more. That is what makes the picker testable
without a bot at all.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------------------------------------------------------------------
# Callback payloads
# ---------------------------------------------------------------------------
# Telegram caps callback_data at 64 bytes. A prefix plus a 36-character UUID
# fits with room to spare; anything longer than an id does not belong here.
CB_SWITCH = "sw"
CB_SUSPEND = "sp"
CB_NEW = "new"

# `new` carries no id, so it can never be mistaken for one: the handler splits
# on the separator and a payload with no second field is not a target.
CB_SEP = ":"


def encode(action: str, session_id: str = "") -> str:
    """The payload a button carries. Ids travel whole, never as positions."""
    return f"{action}{CB_SEP}{session_id}" if session_id else action


def decode(payload: str) -> tuple[str, str]:
    """Split a tapped payload into its action and target id.

    An action with no id yields an empty target rather than a guess, which is
    what keeps `new` from ever resolving to a conversation.
    """
    action, _, target = (payload or "").partition(CB_SEP)
    return action, target


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# The dots Telegram will actually render, with the colour each one reads as.
# A label's colour is a free hex value chosen at the terminal tile, so it will
# usually sit between these; the nearest one is the honest answer.
_DOTS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("\U0001f534", (0xE0, 0x1E, 0x25)),  # red
    ("\U0001f7e0", (0xF1, 0x8F, 0x24)),  # orange
    ("\U0001f7e1", (0xFD, 0xCB, 0x2E)),  # yellow
    ("\U0001f7e2", (0x5C, 0x91, 0x3B)),  # green
    ("\U0001f535", (0x34, 0x81, 0xCC)),  # blue
    ("\U0001f7e3", (0x88, 0x59, 0xA3)),  # purple
    ("\U0001f7e4", (0x7A, 0x4E, 0x2C)),  # brown
    ("⚪", (0xE8, 0xE8, 0xE8)),      # white
)
DEFAULT_DOT = "⚪"


def dot_for_color(color: object) -> str:
    """The nearest renderable dot to a label's hex colour.

    Anything that is not a parseable hex triple — absent, blank, a number, a
    word — gets the default rather than raising. A label the operator set at
    the tile must never be the reason a picker fails to draw.
    """
    if not isinstance(color, str):
        return DEFAULT_DOT
    value = color.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return DEFAULT_DOT
    try:
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return DEFAULT_DOT
    return min(
        _DOTS,
        key=lambda d: (r - d[1][0]) ** 2 + (g - d[1][1]) ** 2 + (b - d[1][2]) ** 2,
    )[0]


def button_text(session: dict, labels: dict) -> str:
    """What one conversation's button says.

    The operator's own label wins over the gateway's generated summary — they
    named it, and a name they chose is the only thing on the button they can
    predict. Everything falls back rather than raising, because a malformed
    row must cost its own button's prose, not the whole picker.
    """
    session_id = str(session.get("id") or "")
    label = labels.get(session_id) or {}
    if not isinstance(label, dict):
        label = {}
    name = (label.get("name") or "").strip()
    caption = name or (session.get("summary") or "").strip() or "Untitled"
    dot = dot_for_color(label.get("color"))
    where = ""
    # A conversation another surface is holding has to read as elsewhere:
    # tapping it *moves* it here, ending the process at the other end, and
    # that is not what an unadorned button looks like it will do. An adoptable
    # row whose surface the gateway did not name still says so, because the
    # move happens either way.
    if session.get("adoptable"):
        where = f" — on {session.get('surface') or 'another surface'}"
    return f"{dot} {caption}{where}"


def build_session_rows(
    sessions: list[dict], labels: dict | None = None
) -> list[list[InlineKeyboardButton]]:
    """One button per conversation, in the order the gateway returned them.

    The gateway decides the order — it is the party that knows what was last
    active — and re-sorting here would mean the picker and every other surface
    disagree about which conversation is on top. Each row carries switch and
    suspend for that conversation; the new-session button is always last, so
    an empty list is still a usable picker rather than a dead end.
    """
    labels = labels or {}
    rows: list[list[InlineKeyboardButton]] = []
    for session in sessions or []:
        session_id = str(session.get("id") or "")
        if not session_id:
            continue
        rows.append([
            InlineKeyboardButton(
                button_text(session, labels),
                callback_data=encode(CB_SWITCH, session_id),
            ),
            InlineKeyboardButton(
                "⏸",
                callback_data=encode(CB_SUSPEND, session_id),
            ),
        ])
    rows.append([InlineKeyboardButton("➕ New session", callback_data=encode(CB_NEW))])
    return rows


def build_session_keyboard(
    sessions: list[dict], labels: dict | None = None
) -> InlineKeyboardMarkup:
    """The rows, wrapped for the send call. No decisions of its own."""
    return InlineKeyboardMarkup(build_session_rows(sessions, labels))


def rename_body(new_name: object, existing: dict | None) -> dict | None:
    """The label PUT body for a rename, or ``None`` when nothing should be sent.

    The terminal's label route deletes the row outright when name and colour
    are both blank, so a bare `/rename` with no text — the easiest thing in the
    world to type by accident — would silently erase a name set at the tile.
    An empty name is therefore a refusal here rather than a write there.

    A rename changes the name and nothing else: the colour the operator picked
    survives, because they picked it in a different place for a different
    reason and the rename never asked about it.
    """
    if not isinstance(new_name, str):
        return None
    name = new_name.strip()[:40]
    if not name:
        return None
    existing = existing if isinstance(existing, dict) else {}
    return {"name": name, "color": (existing.get("color") or "")}
