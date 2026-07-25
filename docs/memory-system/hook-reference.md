# Session hook inventory — what fires when

Snapshot of the hooks the memory system depends on. Hooks live at
`~/.claude/hooks/` on the host — edit IS deploy, no staging mirror.
Containerised minds in upstream hive-mind keep their own copies under
`minds/<name>/.claude/hooks/` (or `.codex/hooks/` for Codex minds).

## UserPromptSubmit (every user turn)

- **`contextual_retrieval.sh`** — pulls standing rules + cosine
  similarity matches + known-persons cue from lucent and injects them
  as `systemMessage`. See [`contextual-retrieval.md`](contextual-retrieval.md).
- **`time_inject.sh`** — injects the current date/time as a fresh anchor
  on every turn (date stamps drift otherwise).

## Stop (every assistant turn complete)

- **`auto_remember.sh`** — captures the last user+assistant turn.
  Branch A runs the multi-class insert sweep (feedback / person /
  tech, see [`insert-sweep.md`](insert-sweep.md)). Branch B runs
  soul self-reflect against the Mind node's `soul_values`. Detached
  subshell — the hook returns instantly.
- **`rotation_check.py`** — char-counts the transcript ÷ 4. Over
  `ROTATION_TOKEN_THRESHOLD` (default 100k) → map-reduce summarize
  via Ollama, write `data/session-state.json`, POST rotation-memory
  to NS, POST `/clear` to rotate. Under threshold → exit. See
  [`session-rotation.md`](session-rotation.md).

## SessionStart

Prompt composition (soul + decay-weighted recent + session-memory
carry-forward) is done **NS-side** by
`comms/bootstrap_loader::compose_prompt_blocks` and shipped to the mind
in the dispatch payload — there is no local SessionStart hook doing it.
Standing rules are **not** part of this composition; they are
injected per turn by `contextual_retrieval.sh` (see above). The mind
passes the composed string straight to
`claude --append-system-prompt`.

The one local SessionStart concern is `session-state.json` injection,
handled by the rotation cycle's `SB` step above.

## Identity convention in hooks

Every hook that writes a `mind_id` field on the lucent API reads
`MIND_ID` (the UUID from `.env`) — never `MIND_NAME`. See the Identity
convention section of `CLAUDE.md`.
