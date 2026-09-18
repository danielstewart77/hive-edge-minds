"""Per-process bot helpers — chat-id serialization and UI formatting.

In-process ``asyncio`` primitives keyed by chat-id, so two incoming
messages from the same chat can't both kick off Claude in parallel.
Lives in the bot's Python process; not shared via NS.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime

_locks: dict[int, asyncio.Lock] = {}
_chat_queues: dict[int, asyncio.Queue] = {}

# Picker messages whose one action has already been taken.
#
# A picker is single use. The enforcement the operator actually sees is the
# keyboard being removed — a message with no buttons cannot produce a tap at
# all — but that removal is an HTTP call to Telegram, and the whole reason
# this exists is that those calls fail. So the claim is taken *here* first,
# synchronously, before anything is awaited: two taps that arrive before the
# edit lands are ordered by this dict, not by the network.
#
# Bounded, because a long-lived bot draws a lot of pickers. Eviction can only
# make a very old picker claimable again, and a very old picker has had its
# keyboard removed, so there is nothing left on it to tap.
_MAX_REMEMBERED_PICKERS = 512
_spent_pickers: "OrderedDict[tuple[int, int], None]" = OrderedDict()


def claim_picker(chat_id: int, message_id: int) -> bool:
    """Take the single action a picker message is allowed, if it is still there.

    ``True`` means this caller owns the tap and should act on it. ``False``
    means the picker was already used and this tap must do nothing but say so.

    Contains no ``await`` by construction. An async claim would let two taps
    on one picker both read "unclaimed" before either wrote, which is the
    precise race that let a second ``New session`` tap destroy the
    conversation the first one had just created.
    """
    key = (chat_id, message_id)
    if key in _spent_pickers:
        return False
    _spent_pickers[key] = None
    while len(_spent_pickers) > _MAX_REMEMBERED_PICKERS:
        _spent_pickers.popitem(last=False)
    return True


def _reset_pickers() -> None:
    """Test helper — forget every claim so a test starts from a clean slate."""
    _spent_pickers.clear()


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
