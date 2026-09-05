# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Project Overview

**hive-edge-minds** is a single-mind installation of the
[hive-mind](https://github.com/danielstewart77/hive-mind) system: one AI mind
on one machine, connected to a hive's shared services (the `hive-comms`
gateway and `hive-lucent` memory containers) over HTTP+bearer. The repo ships
machinery only — a mind's identity (`minds/<name>/`, `souls/<name>.md`,
`.env`, `config.yaml`) is per-host and gitignored.

An edge mind declares a **role** (`operator` — full host access by design;
`sandboxed` — connected but not operating the machine) and a **deployment**
(`systemd`, `container`, or `windows-task`). `setup.sh` scaffolds the mind
from `minds/example/` and emits the matching installer. It never runs sudo
and never starts anything.

### Process model

One deployment unit runs **one Python process**
(`launch_mind_server_and_bots.py`), hosting the mind backend and the surface
bots in-process via `asyncio.gather()`:

- `mind_server.app` — the mind's local backend (HTTP + pty-attach WS)
- `bots.telegram_bot` / `bots.discord_bot` — surface clients

```
                ┌──────────────────────────────────────────────────┐
                │  the edge mind  (one Python process)             │
                │                                                  │
   Telegram ──► │  telegram_bot ──► COMMS_URL ──► mind_server.app  │
                │              (hive-comms)            │           │
                │                                      │  spawns   │
                │                       claude / codex subprocess  │
                │                                                  │
                │  memory hooks ──► HTTP+bearer ──► hive-lucent    │
                └──────────────────────────────────────────────────┘
```

`mind_server.py` is pure spawn/IO — **no prompt composition in the mind**.
hive-comms composes the system prompt (soul from the KG + decay-weighted
recent + session-memory carry-forward) and ships it as
`system_prompt_blocks` in the dispatch payload; the mind passes it straight
to the harness. Standing rules and contextual memory ride in per turn via
UserPromptSubmit hooks configured at the harness user-config level.

### Harness adapters

`mind_templates/<harness>.py` is instantiated to
`minds/<name>/implementation.py` by `setup.sh` (a `MIND_NAME` string
substitution). `mind_server` loads `minds.$MIND_NAME.implementation` and
routes by shape: a `send` coroutine plus session-id `kill` means the
per-turn path (codex); a long-lived process from `spawn` means the
stream-json path (claude).

There are two templates, not one per harness-and-provider pair. The provider
is a separate axis, named in `runtime.yaml` and resolved through
`config.yaml`'s `providers:` block: `ProviderRegistry.get_provider(model)`
returns the env overrides each spawn applies. The model argument is accepted
and ignored — the provider comes from the mind's own `runtime.yaml`, never
from the model name, since a model named by its inference-proxy deployment
name (`claude-opus-5`, not `opus`) carries no vendor hint a short-alias
lookup could key on. An Ollama-backed mind is the same harness with a
different provider, and keeps its browser terminal — which is why no
`*_ollama` template exists to lose it.

- `claude_cli` — long-lived `claude --stream-json` per session, plus
  `spawn_pty` for the browser terminal. Ollama needs nothing beyond the
  provider's `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` overrides.
- `codex_cli` — one `codex exec --json` subprocess per turn, plus
  `spawn_pty` for the browser terminal. Codex has no base-URL environment
  variable, so a non-default provider is declared as a
  `model_providers.<mind>_ollama` config block and selected with
  `model_provider` — `_provider_args` builds those `-c` flags, and the pane
  caches them because a rotation respawns it with no registry in hand.
  Ollama is reached through the inference proxy, never as a bare daemon, and
  the proxy answers 401 without a bearer key — so `env_key` is always
  declared, unconditionally, rather than only when a key happens to be
  visible in the provider block or the ambient environment.
  Codex mints its own thread ids and
  cannot adopt the gateway's conversation id; hive-comms persists that
  provider-native id as `harness_sid`, while the edge mind keeps a local
  disk-backed safety copy. A failed or incomplete turn clears both so the
  next turn never resumes a broken one. POSIX spawns use `start_new_session`
  + `killpg`
  (the node wrapper's rust child must not orphan to PID 1); Windows uses
  CREATE_NO_WINDOW (hidden console inherited by children — never
  DETACHED_PROCESS, which makes every console child pop a visible conhost
  window).

### Conversation ids have exactly one origin

hive-comms mints the conversation id when the session row is written. Every
spawn is handed that id: `--resume` when a transcript exists, `--session-id`
when it's the conversation's first process. A mind handed no id raises
rather than inventing one. Surfaces therefore share one conversation by
construction. Codex additionally reports its own thread id, which hive-comms
stores separately as `harness_sid` and returns on every spawn and terminal
attach; the session's identity remains the gateway's id.

### runtime.yaml is read every boot, and written over HTTP

`minds/<name>/runtime.yaml` is the durable truth about a mind; the broker's
`minds` row is a cache of it. `mind_server` re-registers from the file on
every start (`runtime_config.registration_payload` → `POST /broker/minds`,
an upsert on `mind_id`), which is what makes the file authoritative rather
than an install-time artifact. Registration failure is logged, never fatal.

`GET /runtime` reports the allowlisted configuration; `PATCH /runtime` sets
`default_model` and, when given, `provider` by rewriting each field's line in
place (line substitution, not a YAML round-trip — a dump would strip the
comments). Both fields land in one write, so a mind is never left holding a
provider that doesn't host the model beside it. The write is guarded
by `MIND_ADMIN_TOKEN`, falling back to `COMMS_ADMIN_BEARER_TOKEN`; neither
configured means the route refuses with 503 rather than opening. The hive
console uses these two routes for every mind — a container in the stack, a
bare-metal mind here, or a mind on another machine — writing the file first
and the broker row second, so a restart can't undo the edit.

`GET /models` is the admin-guarded counterpart the console's picker reads:
`models_api.build_catalog` relays the inference proxy's own listing for this
mind's proxy credential, naming its harness on the request
(`/v1/models?harness=claude`, or `codex`) rather than implying it by which
endpoint it asked on — a harness that speaks every wire has no endpoint that
could identify it, and a listing route per harness is how one model ends up
offered to one caller and invisible to another for no reason anybody can see.
Omitting the parameter returns the union, so what the picker offers is exactly
what the mind's key may address and its harness can actually send — no vendor
mapping lives on this side of the call. An unreachable proxy yields an empty
list rather than raising, since the console needs "nothing offered" and
"the mind is down" to read differently.

The model itself is never defaulted. A spawn or an `attach-pty` that arrives
without one is refused: comms resolves it per session from the broker row,
and a mind quietly substituting a house favourite is how a wrong model goes
unnoticed for weeks. A rotation carries the conversation's own model
forward — the pty handle remembers what its pane started on — so editing a
mind's default never moves a live conversation. That default is for the next
conversation.

### Browser terminal (tmux-backed)

`mind_server.py` exposes `WS /sessions/{id}/attach-pty`, bridging raw bytes
between an xterm.js tile and an interactive harness CLI (`claude` or
`codex`, per the mind's template — both templates implement `spawn_pty`).
**The conversation lives in tmux; a tile is a client.** `spawn_pty` starts
the CLI inside a tmux session on a dedicated socket, then attaches a `tmux
attach-session -d` client in a pty of the tile's geometry — ending the
client detaches the view without touching the conversation, and
re-attaching joins the same session rather than starting a rival CLI
process. tmux owns the screen model and history: it repaints on attach and
on live resize, which is why no scrollback ring, VT emulator, or snapshot
painter exists here. `_take_controlling_tty` (setsid + TIOCSCTTY) makes
SIGWINCH actually reach the app — without a controlling terminal a resize
sets winsize and signals nobody. The socket carries a NUL-byte heartbeat
every 5s so half-open mobile connections are detectable. For the claude
harness, `CLAUDE_CODE_DISABLE_AGENT_VIEW=1` is set in the pane: the
harness's agent view is a second session picker inside a surface that
already has one, and it re-hosts the conversation in a nested pty at the
wrong geometry. For the codex harness, a fresh terminal launches bare
`codex` (no thread id yet) rather than pre-minting one through `app-server`:
`app-server`'s `thread/start` RPC returns a thread id and an intended
rollout path without actually writing that rollout file to disk, so
`codex resume <id>` against a pre-minted id fails with "No saved session
found." A background daemon thread polls `CODEX_HOME/sessions/` for the one
new rollout file codex itself writes once the user's first real turn
begins, extracts the thread id from its filename, and reports it via the
same `harness_sid` path the per-turn chat flow uses — mirroring how that
flow already captures `thread.started` in real time. Every later reattach
launches `codex resume <id>` against that discovered id — unless that
thread's rollout no longer exists under this `CODEX_HOME` (a migration or
a redeploy onto a fresh volume left `harness_sid` and the disk safety copy
pointing at a thread minted elsewhere), in which case `_rollout_exists`
discards it and the terminal falls back to the fresh-terminal path instead
of handing back a pane that dies within a second of tmux starting it.
`app-server` has its own arg parser and rejects `--profile` outright —
unlike plain `codex`/`codex exec` invocations — so both paths read the
config profile from `CODEX_HOME`'s `config.toml` directly instead.

The process ends only on `DELETE /sessions/{id}`. A terminal is never
collected for being unattached — that is the normal state between tiles,
and a conversation must not be destroyed by its user's absence. A reaper
runs, but only to drop registry entries for terminals whose harness has
already exited. A turn in flight survives a closed tab.

The session row is exempt from hive-comms' stale-session sweep for the same
reason. That sweep skips anything holding a live subprocess, but a terminal's
harness lives in tmux where comms tracks no process for it — so the row would
look abandoned however hard the pane was being used, and suspending it closes
the next attach with 4411 while the conversation carries on behind it.
Terminal turns also keep `last_active` current through `record_turn`, which
is the only write a pty turn ever makes.

A rotation replaces the conversation, not the session and not the terminal.
The session row is permanent — the tile, its label, the turn ledger and the
`active_sessions` binding are all keyed to `sessions.id` — so what rotates
under it is the harness conversation. Rotation arms a flag that hive-comms
consumes on the conversation's next user turn, but keystrokes are raw bytes:
the terminal never calls `send_message`. What it has instead is the pane's
own `UserPromptSubmit` hook, so a terminal rotation is split in two.

`arm_rotation` *stages*: told by the hook that it is speaking for a pane, it
composes the carry-forward, mints a `claude_sid` it does not start, stores
both on the row and marks `rotation_armed = 2`. The pane is not touched — the
user keeps typing in the conversation they can see. The hook reports that
surface because it is the only party that knows: `owner_ref` cannot answer,
since a Telegram conversation adopted into a terminal keeps its chat
ownership while living in a pane, and arming that for a successor row would
retire the session out from under the pane holding it. `POST /sessions/fire-rotation`,
called by the hook on the next **typed** message, is what swaps: it hands
`POST /sessions/{id}/rotate-pty` the stored seed with that message
concatenated, and `rotate_pty_session` `respawn-pane -k`s the pane onto a
fresh harness process carrying it, renaming nothing and killing nothing, so
the attached tmux client — and with it the pty, the proxied socket and the
browser tile — is never disturbed. `mind_server` repoints the pty handle's
`claude_sid`, and hive-comms writes the same id onto the same row. The user
keeps typing in the same pane under the same session id while the context
behind it resets. tmux targets are prefix-matched, so every session lookup
uses the `=` exact-match form; without it one id can answer for another's
pane.

The two armed values are not interchangeable. `1` is a chat surface,
finalized by `send_message` into a successor row; `2` is a staged terminal,
whose swap keeps the row. A staged terminal adopted by Telegram would
otherwise reach `_finalize_rotation` and be retired for a successor —
destroying the permanent session row, its label and its ledger binding,
which is the outcome in-place rotation exists to prevent. So `send_message`
tests the value rather than its truth. The reverse case is the release: only
a successful fire clears a `2`, and only the pane's own hook can fire one, so
adopting the conversation into Telegram — which kills that pane — would leave
it over its threshold and unable to rotate at all. Releasing a terminal
therefore downgrades a staged rotation to the chat value, which is the only
turn it will get from then on.

A staged rotation reaches the successor as its **opening user turn**, not as
`--append-system-prompt`: the user's own message rides in it, and a system
prompt submits nothing and reaches no transcript, so the pane would open at
an empty prompt with what they typed gone. `_seeded_pane_command`'s
`as_user_turn` picks the entry point; a fresh terminal still takes its seed
as a system prompt, which is where standing context belongs.

The carry-forward travels in a file, never in the tmux command: a composed
prompt is tens of thousands of characters, tmux rejects a long
`respawn-pane` with "command too long", and Linux caps one argv entry at
`MAX_ARG_STRLEN` (128 KiB) regardless. The pane runs a one-line `sh -c` that
reads the seed, deletes it and `exec`s the harness with it; `_capped_seed`
trims anything past 120,000 **bytes** to its tail. Bytes, because that is
what the kernel counts: a carry-forward quoting a TUI transcript carries
box-drawing and arrows, and a few per cent of those puts 120,000 characters
over the limit — where exec fails inside the pane after tmux has returned 0
and the gateway has already written the successor's id, so the rotation is
recorded as successful on a pane that is dead. A seed that cannot be read at
all starts the harness *unseeded* rather than on an empty prompt, for the
same reason: an unseeded terminal is recoverable from the session row below,
and a dead pane is not. A mind reporting no live
terminal writes nothing at all — a fresh `claude_sid` with no process behind
it would strand the session on a conversation that was never seeded.

That file is one process's opening context and is deleted as it is read — so
until the rotated conversation's first turn lands, nothing on disk remembers
what the rotation composed. hive-comms therefore stores it on the session row
(`carry_forward`, keyed to `carry_forward_sid`) at *stage* time, minutes
before the pane is respawned and however long the user takes to reply. The
row is the only thing that outlives both the composing process and a
`skippy.service` restart in the gap. What the fire then stores is what was
actually *delivered* — the summary with the typed message on the end, not the
summary alone — because a tile that dies before that first turn completes
reattaches asking for it, and handing back the summary by itself loses the
question the user asked. A
mind starting a terminal asks `GET /sessions/{id}/carry-forward?claude_sid=`
and re-applies whatever it is owed. A completed turn clears the row — it is
the only evidence the rotation took, and from then on `--resume` carries the
context by itself — so both turn paths clear it: `record_turn` for the
terminal's Stop hook and `send_message` for a conversation adopted by a chat
surface. Each clears only a seed composed for the conversation the turn was
typed into, since composition runs for minutes and a straggling Stop child
can still be reporting a pre-rotation turn after the new seed is written.

Every clearing signal is nonetheless fire-and-forget, so the seed also
expires on its own after `CARRY_FORWARD_TTL_SECONDS`: one dropped POST would
otherwise arm it permanently, and a reattach weeks later would replay a dead
conversation's context over a live one.

The route is admin-guarded — the body is soul, recent memory and the last
exchange in one string — which is also why the column is stripped from every
bulk session listing: those answer to the service token every surface bot
holds, and a `SELECT *` would hand the same blob to all of them. A mind that
cannot reach the gateway opens an unseeded terminal rather than none, since
the transcript is the primary recovery path. A live terminal is never asked:
its harness is already running, so the answer could only be discarded, and
the round trip would delay every reattach.

Chat-surface rotation still retires the session for a successor row (there
is no pane to respawn), and `kill_session` publishes `rotated_to` on the
`session_closed` event so an attached observer reattaches via close code
**4412** with the successor id as the reason. Bare 4410 keeps its meaning:
ended, no successor.

Terminal turns also have to reach the `session_turns` ledger, which is what
`GET /sessions/late-turns` reads to fold turns typed during the rotation's
multi-minute background window into the new conversation's
`<pending-continuation>`.
`send_message` writes that ledger for chat surfaces; the pty bridge can't, so
the Stop hook POSTs each completed turn to `POST /sessions/record-turn` on
every fire when `HIVE_SURFACE=terminal`.

A staged rotation therefore spans three hook processes, none of which share
memory and any of which the respawn can kill, so the handoff is on disk under
`data/auto-remember/`, keyed by `claude_sid` (one host runs several panes
under one harness config). The Stop hook writes its marker *before* the
transcript read and the Ollama calls — those take 90 seconds to 3.5 minutes,
and staging after them meant a user who replied promptly replied into a
conversation that was not yet pending anything. Composition promotes the
marker to ready; a fire that beats it waits briefly and then gives up rather
than holding the prompt. Because the marker goes down before the work, every
way *out* of that work — a missing `mind_id`, a failed memory write, an
Ollama timeout — is a way to leave it saying "composing" with nothing left to
promote it, so the composing process drops its own marker on whichever exit
it takes. One that survived would make every later message wait on a summary
that is never coming, for a day.

The fire hook will not act on a prompt the user did not type. A finished
background agent reports in through the same prompt pipeline and fires every
`UserPromptSubmit` hook, so without that gate the pane rotates itself while
nobody is there and the successor opens on notification XML. There is no
`promptSource` on the payload to ask, so the envelope tag is the signal. Nor
will it fire while `background_tasks` — which rides on every Stop and
SubagentStop payload, and is snapshotted for the fire hook because
`UserPromptSubmit` lacks it — still lists something running: `respawn-pane -k`
kills the pane's process group and every background agent and shell in it.

It never holds the prompt. `UserPromptSubmit` gates submission, so every
second spent in this hook is a second the pane sits frozen with the message
unsent and nothing on screen saying why — and composition runs to six
minutes, which is not a wait, it is a hang. A message that beats the summary
waits seconds for it and then goes through regardless, with the rotation
still staged for the next one. Nor does the hook wait on the respawn it
triggers: it cannot both do that and report a decision back to the process
the respawn kills. The message goes to the harness running now; if the
rotation lands, that answer is replaced mid-flight, and if it doesn't, the
answer stands.

The marker is cleared by the fire that succeeds, and only by that one. A
refused or unreachable gateway leaves it staged, so the next typed turn
retries instead of paying another six minutes for a seed the row already
holds. Keeping it that long is safe because the successor runs under a
*different* conversation id and reads a different marker file — a marker
outliving the swap cannot rotate the fresh conversation away. What the
successor's own turn could collide with is a second prompt queued behind the
first, so a fire takes the marker under a claim: two respawns of one pane
onto one conversation id race over a single seed file, and the loser's
message is gone.

### Terminal voice (transcript tailing, not the pty bytes)

The tile's speaker reads assistant events off the gateway's session event
stream, and exactly one thing publishes those events — `send_message`, the
chat path. A conversation hosted in a pty therefore publishes none by
default: the pty bridge moves raw rendered bytes (ANSI, an alternate
buffer, repaints), not the structured text a speaker could read aloud.

`pty_voice.py` gives a live terminal the same voice a chat surface gets, by
tailing the harness's own transcript instead of parsing the screen.
`SessionVoice` keeps one `TranscriptTailer` per live terminal, swept every
`SWEEP_INTERVAL_S` (0.5s) by `mind_server`'s `_pty_voice_sweep` background
task, and posts each new prose block to `POST /sessions/{id}/pty-text` on
hive-comms via `_post_pty_text` — a best-effort call, logged at debug and
never raised, since a gateway that's down should cost one block of audio
and nothing else. The sweep runs for every live terminal regardless of
whether a tile is attached or its speaker is on; that state is the
browser's to know, and tracking it here would mean holding a copy of
per-tile UI state the mind has no way to keep current.

A tailer opens at end-of-file when it first sees a conversation (so
attaching to a session with hours of history doesn't read the past aloud),
but re-opens from the top when the conversation id under an
already-followed session changes — that's a rotation, and everything in
the new conversation is new. Only `text` content blocks are read; `thinking`
is skipped because the harness writes it empty to disk regardless, and
`tool_use`/`tool_result` are skipped because reading a diff aloud isn't
speech. The browser strips fenced code blocks from what it sends to the
voice server (`terminal-routing.js`'s `speakable`) while leaving them on
screen, and silences an unterminated fence entirely rather than speaking a
half-written command. hive-comms gates its own chat-path publishing on
`owner_type` so a Telegram-driven turn isn't spoken twice — once by
`send_message`, once by the tailer reading the same transcript.

### Cross-surface session pickup

Telegram's `/sessions` lists conversations another surface is holding
(flagged with the surface label); `/switch` adopts one: the mind releases
the terminal (`POST /sessions/{id}/release?surface=terminal` kills the tmux
session and nothing else), ownership retargets, and the stream-json process
respawns with `--resume`. The transcript *is* the handover. The rule
underneath is one live harness process per conversation. Telegram turns
that arrive while a terminal tile is open are mirrored into the attached
socket by `_mirror_turn_to_pty` (a live overlay; tmux can't be told about
bytes it didn't produce).

### Project instructions per harness

Claude reads `CLAUDE.md`; codex reads `AGENTS.md` and ignores `CLAUDE.md`
entirely. `AGENTS.md` is therefore a tracked symlink to `CLAUDE.md` — one
source, both harnesses. Codex resolves no `@path` imports inside it, so there
is no split-file equivalent of `CLAUDE.local.md` at the repo root; a codex
mind's per-install notes go in `$CODEX_HOME/AGENTS.md`, which codex loads
alongside the repo file.

### Skill sync

A skill exists twice: `specs/skills/<harness>/` is the tracked source that
travels with a clone, split into `claude/` and `codex/` subdirectories
because Codex honours a subset of Claude's frontmatter — the files could be
shared, but writing them separately keeps each one honest about what its
harness actually reads. The harness reads a different directory entirely
(`$CLAUDE_CONFIG_DIR/skills` or `$CODEX_HOME/skills`), and that installed
copy is the one that runs. Nothing keeps the two in step, so a mind drifts
from the repo the moment anyone edits a skill in place — a feature, since
that is how a mind gets a skill tuned to its own job.

`skills_sync.py` is the mind's own view of both sides, since only the mind's
own filesystem can see both. `mind_server.py` exposes it as `GET /skills`
(one row per skill, reporting `same` / `differs` / `not_installed` /
`local_only` / `unreadable`), `GET /skills/{name}/diff` (unified diff, repo
against installed), `POST /skills/{name}/install` (copy repo onto mind —
apply and revert are the same operation), `POST /skills/{name}/write-back`
(copy mind onto repo; nothing is committed, the write lands in a working
tree still needing review and push), and `DELETE /skills/{name}` (remove the
mind's copy only). Same shape as `runtime.yaml`'s registration: a container,
a bare-metal mind, and a mind on another machine are one code path because
the mind reports its own state rather than a bind mount reaching in.

Every route is admin-guarded, reads included: the listing returns the full
text of every skill the mind runs, on a port reachable across the LAN.

State is a hash of the whole skill directory, not of `SKILL.md`. A skill is
a directory — references, scripts and templates travel with the markdown,
and a copy moves all of it — so comparing only the markdown would call a
skill synced while its scripts had drifted, and the console offers no action
on a synced row. For the same reason `unreadable` is its own state rather
than folding into "absent": the remedy offered for absence is to overwrite
the directory, and doing that to a skill that is merely unreadable destroys
it. A directory the harness cannot list raises rather than returning empty,
because "this mind has no skills" is a sentence the console will otherwise
state as fact.

Copies preserve symlinks and refuse a tree past `MAX_SKILL_BYTES`.
Dereferencing turns a skill carrying a virtualenv into hundreds of megabytes
of materialised interpreter, which a `venv/` gitignore then hides from `git
status` entirely. Staging is a unique temporary directory inside the target,
so two writers sharing one skills directory cannot delete each other's
staging mid-copy and each report success over a truncated tree.

What the harness does with a newly-written skill is the harness's business.
This machinery copies a directory into the one the harness reads; it makes
no claim about when that skill becomes loadable.

### Editing what the harness reads

`skills_sync` moves whole skill directories between the two sides.
`mind_files.py` is the other half of the same page and a narrower thing:
the files themselves, in the two directories the harness actually reads,
listed and edited in place. `mind_server.py` exposes it as
`GET /files/{tree}` (one row per file, with `size` and whether it can be
opened as text), `GET /files/{tree}/content?path=` and
`PUT /files/{tree}/content`. `tree` is `skills` or `hooks` and nothing
else — every other path on the mind's disk is out of reach by construction
rather than by a rule someone has to remember. Admin-guarded, reads
included: a hooks listing names every script the mind runs per turn, and
those scripts carry gateway URLs and bearer usage.

Containment is judged on the *resolved* path, not the spelling. A skill
legitimately carries symlinks — `_copy_skill` preserves them deliberately —
so a lexical `..` check would admit a symlink aimed at `~/.ssh` while
refusing an ordinary file inside a skill. The two trees are walked
differently for the same reason they are separate: `hooks` is a flat
directory the harness reads whole, while the skills root also holds a
mind's own state (`.usage.json` under its lock, `.curator_state` at 0600,
`.archived/`), so only directories holding a `SKILL.md` are descended into
— the same population `skills_sync` reports. What a skill *installed* is
not the skill: `venv`, `node_modules` and `site-packages` are omitted and
**named** in the response, because a listing that quietly drops things
teaches its reader to distrust it about absence.

Containment allows two roots: the tree, and the harness's `plugins/`
directory — a plugin skill is installed as a symlink into the latter, and
refusing it would leave a skill the mind actually runs neither listable nor
openable. `plugins/` rather than the whole config home, because the home
also holds `settings.json`, and a symlink planted in a skill directory
would otherwise turn an editor for skills into an editor for the harness's
own tokens. A symlink resolving outside both is *named* in the response
rather than dropped from it — "this points somewhere I will not follow"
and "this does not exist" are different sentences, and a browser that hid
the first could not be trusted about the second.

The listing samples the head of each file to decide whether it is
editable, tolerating a multi-byte character cut in half at that boundary:
reading every file whole would be gigabytes inside the console's timeout,
and a naive sniff makes editability depend on whether an em-dash happens to
straddle offset 4096.

A save is a temp file and an `os.replace`, carrying the original's mode
across. Truncating in place would let a bash hook that is 60 seconds into a
three-minute run keep reading from its byte offset into a file that no
longer says what it did, and a fresh inode at 0644 is a hook that silently
stops firing on the next turn. The staging file sits at the *tree root*,
not beside the target: a crash between write and rename leaves it behind,
and inside a skill directory `skills_sync` would hash it — pinning that
skill at `differs` forever, whose only offered remedy is the one that
discards the edit. Stale staging is swept on the next save.

Reads and writes pin `encoding="utf-8", newline=""`. Universal-newline
translation is silent in both directions: a CRLF file read and saved back
unchanged rewrites every line, and on a `windows-task` mind a text-mode
write turns an LF shell script into one that dies on its own shebang.

Reads hand back a `revision`, and a save returns the hash of the bytes it
just wrote rather than a re-read — a re-read hands back whatever landed
*after* the save, which the editor would store as its own, so its next save
would pass the check and clobber silently. A save must carry a revision — one that is
optional on the wire is not a check at all, since the next caller to omit
it overwrites silently — and a stale one is refused with 409. The sync table sits directly above this editor and its "Apply repo
copy" replaces the whole directory — without the check, opening a file,
reverting the skill, then saving the buffer silently un-reverts it, and
two tabs do the same thing to each other.

Liveness differs by tree and the console says so rather than promising one
answer: a hook is re-exec'd per event, so an edit to one is live
immediately; a skill's frontmatter is read when a harness process starts,
so a conversation already running keeps the copy it loaded.

### What the repo ships

`specs/skills/<harness>/` carries the skills without which a mind is not a
mind: `self-reflect` (identity from the graph), `rotate-session`,
`end-session`, `save-session`, `remember`, `always-remember` and `memory`.
Everything else — a host's hardware, a person's projects, one machine's
integrations — belongs only in the installed directory and never here.

A shipped skill names no mind and no person. It reads `$MIND_NAME`,
`$MIND_ID` and the lucent and comms variables from the environment the
service unit already provides, and says "the operator" in prose. Anything
else is a skill exactly one install can run.

There are no `{{PLACEHOLDER}}` tokens and no install-time substitution step.
Substitution would leave every installed copy differing from its source
forever, and the whole point of the sync is that `same` means same.

A skill needing repo content — the data-class specs, a stateless tool —
resolves it through `HIVE_PROJECT_DIR`, which `setup.sh` stamps into `.env`
with this checkout's absolute path. A skill needing a helper script carries
it inside its own directory (`remember/remember.sh`), which works because a
skill is a directory and the sync copies all of it. A script in a shared
`scripts/` folder does not travel and is how a skill arrives broken.

## Identity convention

| Variable | Purpose |
|---|---|
| `MIND_NAME` | Display label for logs, paths, tmux session names. **Never written to the hive's memory.** |
| `MIND_ID` | UUID generated once by `setup.sh`, stamped on the mind's KG node. The `mind_id` field on every memory write. |

## Memory architecture

Sessions are throwaway; the hive's KG and vector store carry weight across
rotations. Capture is per-turn via a Stop hook (pure bash + jq + curl,
detached subshell); retrieval is three-tier (standing / initial prime /
contextual per-turn). Hooks live at the harness user-config level
(`~/.claude/hooks/`), not in this repo. Full design:
[`docs/memory-system/`](docs/memory-system/). Data classification:
[`specs/data-classes/`](specs/data-classes/).

## Key design principles

1. **One process, not many** — simpler ops, one log stream.
2. **Memory is the continuity layer** — the Stop hook is the orchestrator.
3. **Hooks > orchestrators** — bash + jq + curl beats a subagent triad for
   per-turn capture.
4. **Per-process env isolation** — env vars set per subprocess, never
   globally.
5. **Harness-native operations first** — before writing code, ask whether
   Bash / Edit / Write / curl / sqlite3 / docker can do it directly. See
   [`specs/harness-native-operations.md`](specs/harness-native-operations.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest        # zero failures is the bar
```

Tests land with code changes — not optional, not a follow-up.

## Adding new tools

Preferred pattern is **stateless**: a standalone script under
`tools/stateless/<name>/` with argparse + JSON stdout, wired to the harness
via a skill. Editable without restart, no process state, no registration
step. Only reach for a persistent service when the tool genuinely needs a
long-lived connection.
