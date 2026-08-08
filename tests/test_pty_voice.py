"""Reading a terminal's prose off the transcript, as it is written.

The pty bridge carries rendered screen bytes — ANSI, an alternate buffer,
repaints that rewrite lines already sent — so the prose a tile should speak
cannot be recovered from what the browser sees. The harness writes the same
turn a second way: one JSON entry per content block, stamped as each block
is produced. That is what these read.
"""
from __future__ import annotations

import json
from pathlib import Path

import pty_voice


def _entry(*blocks: dict, entry_type: str = "assistant") -> str:
    return json.dumps({"type": entry_type, "message": {"content": list(blocks)}})


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _append(path: Path, *lines: str) -> None:
    with path.open("a") as handle:
        for line in lines:
            handle.write(line + "\n")


# ---------------------------------------------------------------------------
# Requirement 1 — prose is spoken as each piece appears
# ---------------------------------------------------------------------------

def test_two_pieces_of_prose_arrive_as_two_pieces(tmp_path):
    """A reply with two prose blocks yields two, at the moment each lands.

    Polled between the writes rather than after both, because "yields two"
    is also true of a tailer that says nothing until the reply is over —
    which is the behaviour being replaced. What separates them is that the
    first block is available while the second does not yet exist.
    """
    path = tmp_path / "conv.jsonl"
    path.write_text("")
    tailer = pty_voice.TranscriptTailer(path)

    _append(path, _entry(_text("Checking the wiring now.")))
    first = tailer.poll()

    _append(path, _entry(_text("The build passed.")))
    second = tailer.poll()

    assert first == ["Checking the wiring now."]
    assert second == ["The build passed."]


def test_history_already_on_disk_is_not_read_aloud(tmp_path):
    """Attaching to a conversation speaks what happens next, not its past.

    A tile opened on a long-running conversation would otherwise have the
    whole transcript read to it from the beginning.
    """
    path = tmp_path / "conv.jsonl"
    _append(path, _entry(_text("Said an hour ago.")))

    tailer = pty_voice.TranscriptTailer(path)
    assert tailer.poll() == []

    _append(path, _entry(_text("Said just now.")))
    assert tailer.poll() == ["Said just now."]


# ---------------------------------------------------------------------------
# Requirement 2 — only prose is spoken
# ---------------------------------------------------------------------------

def test_a_tool_call_and_its_result_are_not_spoken(tmp_path):
    """Only the prose around a tool call is read aloud.

    The transcript files a tool call, its result and the surrounding
    sentences as separate entries, so this is a matter of selecting the
    right block type — and is the reason this reads the transcript rather
    than the screen, where prose and a tool call are the same pixels.
    """
    path = tmp_path / "conv.jsonl"
    path.write_text("")
    tailer = pty_voice.TranscriptTailer(path)

    _append(
        path,
        _entry(_text("Running the tests.")),
        _entry({"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "580 passed"},
        ]}}),
        _entry(_text("All green.")),
    )

    assert tailer.poll() == ["Running the tests.", "All green."]


def test_thinking_is_not_spoken_because_it_is_not_written(tmp_path):
    """Every thinking block on disk is an empty string beside a signature.

    Measured across 2754 of them on this host, none carrying a character.
    The reasoning is attested, never recorded — so this is a fact about the
    transcript rather than a filter that could be relaxed later.
    """
    path = tmp_path / "conv.jsonl"
    path.write_text("")
    tailer = pty_voice.TranscriptTailer(path)

    _append(path, _entry(
        {"type": "thinking", "thinking": "", "signature": "abc123"},
        _text("Here is what I found."),
    ))

    assert tailer.poll() == ["Here is what I found."]


def test_a_line_written_by_halves_is_not_parsed_as_two(tmp_path):
    """A read can land mid-append, and half a JSON object is not a smaller
    one. The partial tail is held until its newline arrives."""
    path = tmp_path / "conv.jsonl"
    path.write_text("")
    tailer = pty_voice.TranscriptTailer(path)

    whole = _entry(_text("A complete sentence."))
    with path.open("a") as handle:
        handle.write(whole[: len(whole) // 2])
    assert tailer.poll() == []

    with path.open("a") as handle:
        handle.write(whole[len(whole) // 2:] + "\n")
    assert tailer.poll() == ["A complete sentence."]


# ---------------------------------------------------------------------------
# Requirement 4 — speech follows the conversation through a rotation
# ---------------------------------------------------------------------------

def test_a_rotated_pane_keeps_speaking_on_its_new_conversation(tmp_path, monkeypatch):
    """A rotation replaces the conversation and leaves the session alone.

    A tailer bound to the session but pinned to the conversation it started
    on would fall silent at exactly the moment the new one began talking —
    and silence after a rotation is indistinguishable from the feature
    being broken.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    project = Path("/srv/mind")
    voice = pty_voice.SessionVoice()

    before = pty_voice.transcript_path("conv-old", project)
    before.parent.mkdir(parents=True, exist_ok=True)
    before.write_text("")
    voice.poll("sess-1", "conv-old", project)          # start following it
    _append(before, _entry(_text("From the old conversation.")))
    assert voice.poll("sess-1", "conv-old", project) == ["From the old conversation."]

    # The rotation. A fresh conversation id under the same session, whose
    # transcript the harness has not written yet — so nothing is skipped as
    # history and its opening words are spoken.
    after = pty_voice.transcript_path("conv-new", project)
    _append(after, _entry(_text("From the new one.")))

    assert voice.poll("sess-1", "conv-new", project) == ["From the new one."]
    assert voice.poll("sess-1", "conv-new", project) == [], "spoke the same block twice"


def test_a_terminal_that_ended_stops_being_followed(tmp_path, monkeypatch):
    """Tailers are dropped with the terminals they belong to, or a mind that
    has run for weeks holds one open file handle per conversation it has
    ever hosted."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    project = Path("/srv/mind")
    voice = pty_voice.SessionVoice()

    path = pty_voice.transcript_path("conv-1", project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    voice.poll("sess-1", "conv-1", project)
    assert "sess-1" in voice._tailers

    voice.retain_only([])
    assert voice._tailers == {}
