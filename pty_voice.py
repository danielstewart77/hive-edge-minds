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


def prose_blocks(line: str) -> list[str]:
    """The assistant prose in one transcript line, and nothing else.

    Returns text blocks only. A ``thinking`` block is skipped because it is
    empty on disk; ``tool_use`` and its ``tool_result`` are skipped because
    reading a file path and a diff aloud is not speech. Both fall out of
    selecting on the block type rather than needing a filter of their own —
    which is the whole reason this reads the transcript instead of the
    screen, where prose and a tool call are the same pixels.
    """
    try:
        entry = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return []
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text") or ""
        if text.strip():
            out.append(text)
    return out


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
        """Prose written since the last call."""
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
        out: list[str] = []
        for line in lines:
            if line.strip():
                out.extend(prose_blocks(line))
        return out


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
            return tailer.poll()
        return known[1].poll()

    def forget(self, session_id: str) -> None:
        self._tailers.pop(session_id, None)

    def retain_only(self, session_ids) -> None:
        """Drop tailers for terminals that are gone."""
        live = set(session_ids)
        for sid in [s for s in self._tailers if s not in live]:
            self._tailers.pop(sid, None)
