"""The Telegram session picker, including sessions held by another surface.

A session started in the browser terminal shows up here so it can be picked
up from a phone. It has to be visibly *elsewhere* — switching to one moves
the conversation rather than opening a second view of it — while your own
sessions stay unadorned.
"""
from bots.telegram_bot import _format_sessions


def _session(session_id: str, **kwargs) -> dict:
    session = {
        "id": session_id,
        "status": "running",
        "summary": "New session",
        "model": "sonnet",
        "last_active": 0,
    }
    session.update(kwargs)
    return session


def test_empty_list_says_so():
    assert _format_sessions([]) == "No sessions found."


def test_own_sessions_carry_no_surface_marker():
    rendered = _format_sessions([_session("aaaaaaaa-1111", surface="telegram", adoptable=False)])

    assert "aaaaaaaa" in rendered
    assert "on terminal" not in rendered
    assert "running elsewhere" not in rendered


def test_a_session_held_elsewhere_says_where():
    rendered = _format_sessions([
        _session("aaaaaaaa-1111", surface="telegram", adoptable=False),
        _session("bbbbbbbb-2222", summary="the desk thing", surface="terminal", adoptable=True),
    ])

    assert "— on terminal" in rendered
    assert "the desk thing" in rendered
    assert "/switch moves one here" in rendered


def test_positions_are_stable_across_both_kinds():
    # /switch and /kill take these numbers, so the ordering shown is the
    # ordering they resolve against.
    rendered = _format_sessions([
        _session("aaaaaaaa-1111", adoptable=False),
        _session("bbbbbbbb-2222", adoptable=True, surface="terminal"),
    ])
    lines = [line for line in rendered.splitlines() if line[:2] in ("1.", "2.")]

    assert lines[0].startswith("1.") and "aaaaaaaa" in lines[0]
    assert lines[1].startswith("2.") and "bbbbbbbb" in lines[1]


def test_adoptable_without_a_surface_label_still_reads():
    rendered = _format_sessions([_session("cccccccc-3333", adoptable=True)])

    assert "on another surface" in rendered
