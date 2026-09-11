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
                text = item.get("text")
                # An MCP server is the one writer here the harness does not
                # control, and `text` holding a dict or a number is its to
                # invent. `join` would raise on it, and a raise in this
                # function costs the whole sweep's batch — the column *and*
                # the speaker go silent for that turn.
                parts.append(text if isinstance(text, str) else json.dumps(item, default=str))
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
    if not isinstance(agent, str):
        agent = None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
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
        if block_type == "tool_use":
            raw_name = block.get("name")
            name = raw_name if isinstance(raw_name, str) else None
            rendered = _rendered(block.get("input"))
            reported = "tool_use"
        elif block_type == "tool_result":
            rendered = _rendered(block.get("content"))
            reported = "tool_result"
        elif block_type == "thinking":
            rendered = block.get("thinking")
            reported = "thinking"
        elif block_type == "text":
            rendered = block.get("text")
            reported = "user" if kind == "user" else "text"
        else:
            continue
        # Every one of these fields is written by something upstream — the
        # harness, or an MCP server answering a tool call. A non-string here
        # used to reach `.strip()` and take the sweep's whole batch with it.
        if not isinstance(rendered, str):
            rendered = _rendered(rendered)
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
            }
        )
    return out


def subagent_transcripts(claude_sid: str, project_dir: Path) -> list[Path]:
    """Every sub-mind transcript this conversation has opened.

    The parent file holds none of their work. The harness gives each
    delegate its own `<sid>/subagents/agent-<id>.jsonl` and writes only the
    dispatching `tool_use` and the final `tool_result` into the parent — so
    tailing the parent alone shows a delegation as one block at the start
    and one at the end, with the minutes between it blank.

    A conversation that has delegated nothing has no such directory, and
    that is the ordinary case rather than a fault.
    """
    parent = transcript_path(claude_sid, project_dir)
    folder = parent.with_suffix("") / "subagents"
    try:
        return sorted(folder.glob("agent-*.jsonl"))
    except OSError:
        return []


def agent_label(transcript: Path) -> str:
    """What to call the sub-mind that wrote this file.

    The harness drops an `agent-<id>.meta.json` beside each one naming the
    job it was spawned for. "refute-edges" tells a reader what the work was;
    "a19ca8c72d907ffca" is a handle and nothing more — which is what is left
    when no meta file exists, since a blank attribution is worse than an
    ugly one.
    """
    stem = transcript.stem
    fallback = stem[len("agent-"):] if stem.startswith("agent-") else stem
    try:
        meta = json.loads(transcript.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        return fallback
    label = meta.get("agentType") if isinstance(meta, dict) else None
    return label if isinstance(label, str) and label else fallback


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
        """Everything written since the last call, typed and attributed.

        One unreadable line costs that line. The offset has already moved
        past the whole batch by the time anything is parsed, so a raise here
        would drop every other block in it permanently — and those blocks
        feed the speaker as well as the dashboard, so the tile would go mute
        for the same turn.
        """
        out: list[dict] = []
        for line in self.poll_lines():
            try:
                out.extend(activity_blocks(line))
            except Exception:
                log.debug("unreadable transcript line in %s", self.path, exc_info=True)
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
        #: Per session, one tailer per sub-mind transcript it has opened.
        self._delegates: dict[str, dict[str, tuple[str, TranscriptTailer]]] = {}

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

        A delegating conversation is several files, not one: the parent, and
        one per sub-mind. New delegates appear mid-turn, so the set is
        re-read each sweep rather than fixed when the conversation opened.
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
            self._delegates.pop(session_id, None)
            out = tailer.poll_blocks()
        else:
            out = known[1].poll_blocks()

        delegates = self._delegates.setdefault(session_id, {})
        for path in subagent_transcripts(claude_sid, project_dir):
            key = str(path)
            follower = delegates.get(key)
            if follower is None:
                # From the top. A delegate's file exists because it was just
                # spawned, so there is no history to replay — and opening at
                # the end would lose whatever it wrote in the half second
                # before this sweep noticed it.
                follower = (agent_label(path), TranscriptTailer(path, from_start=True))
                delegates[key] = follower
            label, tail = follower
            for block in tail.poll_blocks():
                # The file says which delegate wrote it; the meta file says
                # what it was for. A block that arrived unattributed would be
                # rendered as the mind's own work.
                block["agent"] = label
                out.append(block)
        return out

    def forget(self, session_id: str) -> None:
        self._tailers.pop(session_id, None)
        self._delegates.pop(session_id, None)

    def retain_only(self, session_ids) -> None:
        """Drop tailers for terminals that are gone."""
        live = set(session_ids)
        for sid in [s for s in self._tailers if s not in live]:
            self._tailers.pop(sid, None)
            self._delegates.pop(sid, None)
