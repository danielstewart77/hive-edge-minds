# Session hook inventory — what fires when

Snapshot of the hooks the memory system depends on. Hooks live at
`~/.claude/hooks/` or `~/.codex/hooks/` on the host — edit IS deploy,
no staging mirror.
Containerised minds in upstream hive-mind keep their own copies under
`minds/<name>/.claude/hooks/` (or `.codex/hooks/` for Codex minds).

## UserPromptSubmit (every user turn)

- **`contextual_retrieval.sh`** — pulls standing rules + cosine
  similarity matches + known-persons cue from lucent and injects them as
  `hookSpecificOutput.additionalContext` (`systemMessage` is UI-only and
  never reaches the model). See
  [`contextual-retrieval.md`](contextual-retrieval.md).
- **`time_inject.sh`** — injects the current date/time as a fresh anchor
  on every turn (date stamps drift otherwise).
- **`surface_inject.sh`** — emits a `<message-surface>` block from
  `HIVE_SURFACE` so the reply is shaped for where it lands.
- **`prose_reminder.sh`** — keys off the same variable: the spoken-prose
  rule fires for TTS surfaces and unset, and is skipped for `terminal`
  and `local`.

## Stop (every assistant turn complete)

- **`auto_remember.sh`** — captures the last user+assistant turn.
  Branch A runs the multi-class insert sweep (feedback / person /
  tech, see [`insert-sweep.md`](insert-sweep.md)). Branch B runs
  soul self-reflect against the Mind node's `soul_values`. Detached
  subshell — the hook returns instantly.
- **`rotation_check.py`** — char-counts the transcript ÷ 4. Over
  `ROTATION_TOKEN_THRESHOLD` (default 200000) → fork a detached child
  that map-reduce summarizes via Ollama, folds in
  `GET /sessions/late-turns`, POSTs the carry-forward to
  `/sessions/{sid}/rotation-memory`, then `POST /sessions/arm-rotation`.
  Under threshold → exit. Every fire, terminal sessions
  (`HIVE_SURFACE=terminal`) also POST the completed turn to
  `/sessions/record-turn` so the ledger the late-turn fold reads is not
  blind to pty traffic. See
  [`session-rotation.md`](session-rotation.md).
- **`training_capture.sh`** — archives the turn pair for training data.
- **`skill_telemetry.sh`** — records which skills the turn invoked.

## SessionStart

Prompt composition (soul + decay-weighted recent + session-memory
carry-forward) is done **NS-side** by
`comms/bootstrap_loader::compose_prompt_blocks` and shipped to the mind
in the dispatch payload — there is no local SessionStart hook doing it.
Standing rules are **not** part of this composition; they are
injected per turn by `contextual_retrieval.sh` (see above). The mind
passes the composed string straight to
`claude --append-system-prompt`. The rotation carry-forward reaches a
successor the same way — as the `<session-memory>` block of that NS-side
composition — so there is nothing for a local SessionStart hook to do.

## Identity convention in hooks

Every hook that writes a `mind_id` field on the lucent API reads
`MIND_ID` (the UUID from `.env`) — never `MIND_NAME`. See the Identity
convention section of `CLAUDE.md`.
