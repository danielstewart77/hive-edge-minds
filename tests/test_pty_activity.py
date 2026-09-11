"""Everything a terminal turn does, put on the wire as it happens.

The voice path reads prose and drops the rest — a tile that spoke its own
tool calls would read diffs aloud. The dashboard wants the opposite: the
whole turn, tool inputs and results included, from the instant the user
presses enter. Both are read off the same transcript by the same tailer,
which is why the split lives in what each caller asks for rather than in
two readers racing each other over one file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pty_voice


def _line(*blocks: dict, entry_type: str = "assistant", **extra) -> str:
    entry = {"type": entry_type, "message": {"content": list(blocks)}}
    entry.update(extra)
    return json.dumps(entry)


def _kinds(blocks: list[dict]) -> list[str]:
    return [block["kind"] for block in blocks]


# ---------------------------------------------------------------------------
# R1 — a column appears the moment the turn begins
# ---------------------------------------------------------------------------


def test_a_submitted_user_turn_is_reported_as_the_turn_opening():
    """R1. The dashboard opens a column on this and nothing else.

    A terminal publishes no `user` event — its keystrokes are raw bytes —
    so the transcript's own user entry is the only evidence that a turn
    has started before any output exists.
    """
    opening = pty_voice.activity_blocks(
        _line({"type": "text", "text": "run the tests"}, entry_type="user")
    )
    assert _kinds(opening) == ["user"]
    assert opening[0]["text"] == "run the tests"

    # A summary entry is bookkeeping the harness writes for itself. Opening
    # a column on one would put a conversation on screen that nobody started.
    assert pty_voice.activity_blocks(
        json.dumps({"type": "summary", "summary": "earlier work"})
    ) == []


# ---------------------------------------------------------------------------
# R2 — everything appears: prose, thinking, tool calls, tool results
# ---------------------------------------------------------------------------


def test_a_tool_call_is_reported_with_its_name_and_its_whole_input():
    """R2, R3. The command is the thing worth seeing while it runs."""
    blocks = pty_voice.activity_blocks(
        _line(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "pytest -q tests/", "timeout": 120000},
            }
        )
    )
    assert _kinds(blocks) == ["tool_use"]
    assert blocks[0]["name"] == "Bash"
    assert "pytest -q tests/" in blocks[0]["text"]
    assert "120000" in blocks[0]["text"]

    # A thinking block carries no tool call, and the harness writes its text
    # empty on disk regardless — so it yields no tool_use to report.
    thinking = pty_voice.activity_blocks(
        _line({"type": "thinking", "thinking": "", "signature": "abc"})
    )
    assert "tool_use" not in _kinds(thinking)


def test_a_tool_result_is_reported_with_its_body():
    """R2, R3. Half the turn is what came back, not what was asked."""
    blocks = pty_voice.activity_blocks(
        _line(
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": "395 passed, 1 warning",
            },
            entry_type="user",
        )
    )
    assert _kinds(blocks) == ["tool_result"]
    assert blocks[0]["text"] == "395 passed, 1 warning"


# ---------------------------------------------------------------------------
# R3 — nothing is withheld for content
# ---------------------------------------------------------------------------


def test_a_block_under_the_cap_is_untouched_and_one_over_it_is_trimmed():
    """R3. A 300KB file read must not flush the column's history.

    The boundary is in bytes because that is what a transport counts, and a
    transcript quoting a TUI carries box-drawing characters that cost three
    bytes each.
    """
    under = "x" * 119_999
    kept = pty_voice.cap_block(under)
    assert kept["text"] == under
    assert kept["trimmed"] is False

    over = "y" * 120_001
    cut = pty_voice.cap_block(over)
    assert cut["trimmed"] is True
    assert len(cut["text"].encode("utf-8")) <= 120_000


# ---------------------------------------------------------------------------
# R5 — the instructions sent to a sub-mind appear in full
# ---------------------------------------------------------------------------


def test_the_prompt_handed_to_a_sub_mind_is_reported_whole():
    """R5. The dispatch is the instruction; a summary of it is not."""
    prompt = "Audit every template for narration copy."
    blocks = pty_voice.activity_blocks(
        _line({"type": "tool_use", "name": "Agent", "input": {"prompt": prompt}})
    )
    assert blocks[0]["name"] == "Agent"
    assert prompt in blocks[0]["text"]

    # A prompt long enough to be trimmed is still reported as trimmed rather
    # than silently ending mid-instruction.
    long_prompt = "Audit every template. " * 10_000
    trimmed = pty_voice.activity_blocks(
        _line({"type": "tool_use", "name": "Agent", "input": {"prompt": long_prompt}})
    )
    assert trimmed[0]["trimmed"] is True


# ---------------------------------------------------------------------------
# R6 — a sub-mind's own work appears as it happens
# ---------------------------------------------------------------------------


def test_a_sub_minds_own_turn_is_reported_rather_than_dropped():
    """R6. Its final answer is the smallest part of what it did."""
    blocks = pty_voice.activity_blocks(
        _line(
            {"type": "text", "text": "Found three offenders."},
            isSidechain=True,
            agentId="agent-7",
        )
    )
    assert _kinds(blocks) == ["text"]
    assert blocks[0]["text"] == "Found three offenders."

    # An entry that is neither a turn nor an opening has nothing to report.
    assert pty_voice.activity_blocks(
        json.dumps({"type": "system", "subtype": "init"})
    ) == []


# ---------------------------------------------------------------------------
# R7 — a sub-mind's work is attributed to it
# ---------------------------------------------------------------------------


def test_work_from_a_sub_mind_carries_its_agent_and_the_minds_own_does_not():
    """R7. Mixed into one column, the two are indistinguishable."""
    delegated = pty_voice.activity_blocks(
        _line({"type": "text", "text": "done"}, isSidechain=True, agentId="agent-7")
    )
    assert delegated[0]["agent"] == "agent-7"

    mine = pty_voice.activity_blocks(_line({"type": "text", "text": "done"}))
    assert mine[0]["agent"] is None


# ---------------------------------------------------------------------------
# R6, R7 — a sub-mind writes its own file, and it has to be followed
# ---------------------------------------------------------------------------


def test_a_sessions_sub_mind_transcripts_are_found_beside_it(tmp_path):
    """R6. The parent transcript never holds a sub-mind's turns: the harness
    gives each one its own file under `<sid>/subagents/`. Tailing only the
    parent is why a delegated run showed as one block at the end."""
    import os

    project = tmp_path / "proj"
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp_path / "config")
    parent = pty_voice.transcript_path("sid-1", project)
    subagents = parent.with_suffix("") / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a1.jsonl").write_text("")
    (subagents / "agent-a1.meta.json").write_text('{"agentType": "refute-edges"}')

    found = pty_voice.subagent_transcripts("sid-1", project)
    assert [p.name for p in found] == ["agent-a1.jsonl"]

    # A conversation that has delegated nothing has no directory at all, and
    # raising there would take down the sweep for every other terminal.
    assert pty_voice.subagent_transcripts("sid-never-delegated", project) == []


def test_a_sub_mind_is_named_by_its_job_not_by_its_identifier(tmp_path):
    """R7. "refute-edges" says what the work was; "a19ca8c72d907ffca" does
    not, and a column of hex ids is a column nobody reads."""
    meta = tmp_path / "agent-a1.meta.json"
    meta.write_text('{"agentType": "refute-edges", "description": "Refute edges"}')
    assert pty_voice.agent_label(tmp_path / "agent-a1.jsonl") == "refute-edges"

    # No meta file is the ordinary case for an older run; the id is ugly but
    # it is a handle, where a blank is nothing.
    assert pty_voice.agent_label(tmp_path / "agent-a2.jsonl") == "a2"


def test_a_sub_minds_tool_call_reaches_the_sweep_attributed_to_it(tmp_path, monkeypatch):
    """R6, R7 together, at the layer the requirement actually lands on: what
    one sweep of a live terminal returns."""
    project = tmp_path / "proj"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    parent = pty_voice.transcript_path("sid-1", project)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("")
    subagents = parent.with_suffix("") / "subagents"
    subagents.mkdir(parents=True)
    sub = subagents / "agent-a1.jsonl"
    sub.write_text("")
    (subagents / "agent-a1.meta.json").write_text('{"agentType": "explorer"}')

    voice = pty_voice.SessionVoice()
    voice.poll_blocks("s1", "sid-1", project)  # open at end of file

    with sub.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "agentId": "a1",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Grep",
                                "input": {"pattern": "narration"},
                            }
                        ]
                    },
                }
            )
            + "\n"
        )

    blocks = voice.poll_blocks("s1", "sid-1", project)
    assert [b["kind"] for b in blocks] == ["tool_use"]
    assert blocks[0]["agent"] == "explorer"
    assert "narration" in blocks[0]["text"]


def test_one_unreadable_entry_does_not_cost_the_rest_of_the_batch(tmp_path, monkeypatch):
    """R2. The offset has already moved past the whole batch by the time
    anything is parsed, so a raise on one line loses every other block in it
    permanently — and those blocks feed the speaker too, so the tile goes
    mute for the same turn. An MCP server is the one writer here the harness
    does not control, and the shape of what it returns is its own.
    """
    project = tmp_path / "proj"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    parent = pty_voice.transcript_path("sid-1", project)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("")

    voice = pty_voice.SessionVoice()
    voice.poll_blocks("s1", "sid-1", project)

    with parent.open("a") as handle:
        # `text` holding a dict rather than a string used to reach .strip().
        handle.write(_line({"type": "text", "text": {"oops": 1}}) + "\n")
        handle.write(_line({"type": "text", "text": "this still arrives"}) + "\n")

    blocks = voice.poll_blocks("s1", "sid-1", project)
    assert "this still arrives" in [block["text"] for block in blocks]


def test_a_tool_result_from_an_uncontrolled_writer_is_rendered_not_raised():
    """R2. Same failure, one layer down: a `text` key holding a dict."""
    blocks = pty_voice.activity_blocks(
        _line(
            {"type": "tool_result", "content": [{"text": {"nested": 1}}]},
            entry_type="user",
        )
    )
    assert [block["kind"] for block in blocks] == ["tool_result"]
    assert "nested" in blocks[0]["text"]

    # A name that is not a name must not reach a set lookup either.
    odd = pty_voice.activity_blocks(
        _line({"type": "tool_use", "name": ["Agent"], "input": {"prompt": "go"}})
    )
    assert odd[0]["name"] is None
