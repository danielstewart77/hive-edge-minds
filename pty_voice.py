"""A terminal's prose, put on the wire as it is written.

The tile's speaker reads assistant events off the gateway's session event
stream. Exactly one thing publishes those events — ``send_message``, the
chat path — so a conversation hosted in a pty publishes none, and the
speaker is silent for every browser terminal. Before tmux, the terminal ran
the stream-json harness and got the events for free; the pty replaced that
with raw bytes and took the events with it.

The bytes are not the way back. They are a rendered screen: ANSI, an
alternate buffer, repaints that rewrite lines already sent. What the harness
*also* writes is its transcript, one JSON entry per content block, stamped
as each block is produced — prose, thinking and tool calls each in their own
entry. Tailing that gives back the pre-tmux behaviour with no emulator, no
parsing of escape sequences, and a free answer to "was this prose or a tool
call".

Thinking is not available and cannot be made so. Every ``thinking`` block
the harness writes carries an empty ``thinking`` string beside its
signature — measured across 2754 of them on this host, none with a single
character of text. The reasoning is attested on disk, never recorded there.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("hive-mind.pty-voice")

# How often the sweep looks for new prose. This is the latency between a
# sentence being written and it being spoken, so it wants to be well under
# the time it takes to say one — but every tick stats a file per live
# terminal, so it is not free either.
SWEEP_INTERVAL_S = 0.5

# One block's ceiling, in bytes. A tool result can be a whole file, and a
# column that flushed four hundred blocks of history to show one `cat` is
# worse than one that says the read was long. Bytes rather than characters
# because that is what a transport counts, and a transcript quoting a TUI
# carries box-drawing and arrows at three bytes each.
MAX_BLOCK_BYTES = 120_000

# A single read's ceiling. A turn that emits a very large block should not
# be able to pull an unbounded string into memory on one tick; the remainder
# is picked up on the next.
MAX_READ_BYTES = 1_048_576


def transcript_path(claude_sid: str, project_dir: Path) -> Path:
    """Where the claude CLI keeps this conversation.

    ``<config>/projects/<slugified-cwd>/<sid>.jsonl``, the slug being the cwd
    with every ``/``, ``_`` and ``.`` turned into ``-``. Mirrors
    ``implementation._claude_transcript_exists``, which resolves the same
    path to decide ``--resume`` against ``--session-id``.
    """
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    slug = str(project_dir).replace("/", "-").replace("_", "-").replace(".", "-")
    return config_dir / "projects" / slug / f"{claude_sid}.jsonl"


def cap_block(text: str) -> dict:
    """One block's text, trimmed to something a column can hold.

    The tail is kept rather than the head: the end of a command's output is
    where the error is. A trim is reported rather than done silently,
    because a reader who cannot tell truncation from a short result will
    read the wrong conclusion off the screen.
    """
    raw = (text or "").encode("utf-8", "replace")
    if len(raw) <= MAX_BLOCK_BYTES:
        return {"text": text or "", "trimmed": False}
    kept = raw[-MAX_BLOCK_BYTES:].decode("utf-8", "replace")
    return {"text": kept, "trimmed": True}


def _rendered(value) -> str:
    """A tool's input or result as one string, whatever shape it arrived in.

    Inputs are dicts, results are sometimes a string and sometimes a list of
    blocks. The dashboard shows the whole thing either way, so the rendering
    happens once here rather than in every reader.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if value is None:
        return ""
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def activity_blocks(line: str) -> list[dict]:
    """Everything one transcript entry did, typed and attributed.

    Where ``TranscriptTailer.poll`` answers "what should be spoken", this
    answers "what happened" — prose, thinking, the tool call
    with its whole input, the result with its whole body, and the user's own
    submission, which is the only evidence a terminal turn has started
    before it has produced anything.

    Sub-mind work arrives in this same file marked ``isSidechain`` with an
    ``agentId``. It is reported rather than dropped, and carries that id, so
    a column can show whose work it was instead of blending a delegate's
    tool calls into the mind's own.
    """
    try:
        entry = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(entry, dict):
        return []
    kind = entry.get("type")
    if kind not in ("assistant", "user"):
        return []
    agent = entry.get("agentId") if entry.get("isSidechain") else None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []

    out: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        name = None
        dispatch_prompt = None
        if block_type == "tool_use":
            name = block.get("name")
            rendered = _rendered(block.get("input"))
            # The prompt handed to a sub-mind is the instruction itself, not
            # a parameter of one, so it is lifted out whole rather than left
            # inside a rendered argument blob a reader has to dig through.
            if name in _DISPATCH_TOOLS:
                raw_input = block.get("input")
                if isinstance(raw_input, dict):
                    dispatch_prompt = raw_input.get("prompt")
            reported = "tool_use"
        elif block_type == "tool_result":
            rendered = _rendered(block.get("content"))
            reported = "tool_result"
        elif block_type == "thinking":
            rendered = block.get("thinking") or ""
            reported = "thinking"
        elif block_type == "text":
            rendered = block.get("text") or ""
            reported = "user" if kind == "user" else "text"
        else:
            continue
        if reported in ("text", "user", "thinking") and not rendered.strip():
            continue
        capped = cap_block(rendered)
        out.append(
            {
                "kind": reported,
                "text": capped["text"],
                "trimmed": capped["trimmed"],
                "name": name,
                "agent": agent,
                "dispatch_prompt": dispatch_prompt,
            }
        )
    return out


#: Tools whose input *is* a dispatch to another mind.
_DISPATCH_TOOLS = frozenset({"Agent", "Task"})


class TranscriptTailer:
    """Follows one conversation's transcript, yielding prose as it arrives.

    Opens at end-of-file rather than at the beginning. A tile attaching to a
    conversation with history would otherwise have the whole of it read
    aloud from the top, which is not what "speak as it is written" means. A
    conversation that has just been created has nothing behind it, so a
    rotation loses nothing by the same rule.

    A partial trailing line is kept rather than parsed. The harness appends
    whole entries, but a read can still land mid-write, and half of a JSON
    object is not a smaller JSON object.
    """

    def __init__(self, path: Path, from_start: bool = False):
        self.path = path
        self._pending = ""
        if from_start:
            self._offset = 0
            return
        try:
            self._offset = path.stat().st_size
        except OSError:
            # Not written yet — a conversation whose first block is still
            # being composed. Start at zero so nothing is missed when it
            # appears.
            self._offset = 0

    def poll(self) -> list[str]:
        """Prose written since the last call.

        One of the two readers over ``poll_lines``, and they share an
        offset: whichever is called consumes what it read. A caller wanting
        both takes ``poll_blocks`` and filters, which the sweep does.
        """
        return [
            block["text"]
            for line in self.poll_lines()
            for block in activity_blocks(line)
            if block["kind"] == "text"
        ]

    def poll_blocks(self) -> list[dict]:
        """Everything written since the last call, typed and attributed."""
        out: list[dict] = []
        for line in self.poll_lines():
            out.extend(activity_blocks(line))
        return out

    def poll_lines(self) -> list[str]:
        """Whole transcript lines written since the last call."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            # Truncated or replaced under us. Re-reading from zero would
            # speak the file again; the only safe read is what is new from
            # here on.
            self._offset = size
            self._pending = ""
            return []
        if size == self._offset:
            return []
        try:
            with self.path.open("r", errors="replace") as handle:
                handle.seek(self._offset)
                chunk = handle.read(MAX_READ_BYTES)
        except OSError:
            return []
        if not chunk:
            return []
        self._offset += len(chunk.encode("utf-8", "replace"))

        data = self._pending + chunk
        lines = data.split("\n")
        self._pending = lines.pop()          # partial line, or "" after a newline
        return [line for line in lines if line.strip()]


class SessionVoice:
    """One tailer per live terminal, following it through a rotation.

    Keyed by session id and re-targeted when the conversation under it
    changes — a rotation replaces the conversation and leaves the session
    alone, so a tailer bound to the session but pinned to the old
    conversation would go quiet exactly when the new one started talking.
    """

    def __init__(self):
        self._tailers: dict[str, tuple[str, TranscriptTailer]] = {}

    def poll(self, session_id: str, claude_sid: str, project_dir: Path) -> list[str]:
        """This terminal's new prose. See ``poll_blocks`` for everything."""
        return [
            block["text"]
            for block in self.poll_blocks(session_id, claude_sid, project_dir)
            if block["kind"] == "text"
        ]

    def poll_blocks(
        self, session_id: str, claude_sid: str, project_dir: Path
    ) -> list[dict]:
        """Everything this terminal has done since the last sweep.

        One read feeding both consumers. The speaker and the dashboard want
        different subsets of the same entries, and two tailers over one file
        would double the IO to arrive at the same lines.
        """
        if not session_id or not claude_sid:
            return []
        known = self._tailers.get(session_id)
        if known is None or known[0] != claude_sid:
            # A conversation id we have not seen under a session we *were*
            # already following is a rotation, and everything in it is new —
            # read from the top. The first id we see for a session is the
            # other case: a tile attaching to a conversation that may have
            # been talking for hours, where reading from the top means the
            # whole history out loud. The sweep runs every half second, so a
            # rotation whose harness writes its opening block before the next
            # tick would lose exactly the words that explain the rotation.
            rotated = known is not None
            tailer = TranscriptTailer(
                transcript_path(claude_sid, project_dir), from_start=rotated
            )
            self._tailers[session_id] = (claude_sid, tailer)
            return tailer.poll_blocks()
        return known[1].poll_blocks()

    def forget(self, session_id: str) -> None:
        self._tailers.pop(session_id, None)

    def retain_only(self, session_ids) -> None:
        """Drop tailers for terminals that are gone."""
        live = set(session_ids)
        for sid in [s for s in self._tailers if s not in live]:
            self._tailers.pop(sid, None)
