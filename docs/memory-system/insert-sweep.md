# Per-turn insert sweep

What `~/.claude/hooks/auto_remember.sh` runs on every Stop event. Three
independent classification branches against one Ollama call; each branch
that fires writes to lucent. Self-content is handled by Branch B (soul
self-reflect) in the same hook and is not part of the sweep.

```mermaid
flowchart TD
    CHUNK[CHUNK]

    CHUNK --> ISP{"Is person?<br/>Prose about a named<br/>individual in the operator's life"}
    CHUNK --> IST{"Is tech?<br/>Durable fact about code,<br/>config, or system state"}
    CHUNK --> ISS{"Is self?<br/>Identity-shaping content"}
    CHUNK --> ISF{"Is feedback?<br/>Stable preference,<br/>correction, or pushback"}

    ISP --> PEX{"entity_name lookup<br/>GET /graph/query<br/>count==1, type==Person?"}
    PEX -->|yes| PFITS{"Fits KG schema?"}
    PFITS -->|no| VSP[(VS write<br/>data_class=current-state<br/>"[About &lt;Name&gt;] &lt;prose&gt;")]

    IST --> TDC{"data_class<br/>current-state | future-state"}
    TDC --> TFITS{"Fits KG schema?"}
    TFITS -->|no| VST[(VS write<br/>data_class=&lt;tdc&gt;)]

    ISS --> SOUL[Branch B<br/>soul self-reflect]

    ISF --> VSF[(VS write<br/>data_class=feedback)]

    classDef live fill:#cfc,stroke:#080,color:#000
    classDef store fill:#ddf,stroke:#338,color:#000

    class ISP,IST,ISS,ISF,PEX,PFITS,TDC,TFITS,SOUL live
    class CHUNK,VSP,VST,VSF store
```

## How it runs

A single `/ollama/structured` call returns a four-branch verdict:

```json
{
  "feedback": {"present": bool, "content": str, "reason": str},
  "person":   {"present": bool, "name": str, "prose": str, "fits_schema": bool, "reason": str},
  "tech":     {"present": bool, "data_class": "current-state|future-state", "prose": str, "fits_schema": bool, "reason": str},
  "self":     {"present": bool, "reason": str}
}
```

Branches are independent — multiple can fire on one chunk. Lucent's
save-time dedup (cosine ≥ 0.92) absorbs near-overlap when person and
tech both write similar prose.

The classifier prompt injects a compact `<kg-schema>` block fetched
from `GET /graph/schema` on each run. The block lists node types
(required + optional properties) and edge types (endpoints). The
classifier uses it to answer `fits_schema?` per branch.

`fits_schema=true` means the chunk would be a typed KG node/edge
(e.g. *"Gary's phone is 555-1234"*) — those are written by explicit
in-turn skills, never by this autonomous sweep, so the branch logs
the verdict and drops.

## Person branch — the `entity_name` validation

When `person.present=true` and `person.fits_schema=false`, the hook
runs a naive `GET /graph/query?entity_name=<name>` to confirm the name
resolves to exactly one Person node:

- `count != 1` → drop (unknown name, ambiguous match, or non-Person hit).
- `count == 1 && type == "Person"` → write to VS with content
  `"[About <Name>] <prose>"`. The bracketed prefix makes the chunk
  recoverable by the `<known-persons>` cue's prose-recall path.

Unknown names aren't auto-created. Adding a Person to the KG is the
job of an explicit in-turn skill (e.g. `/add-person`), never the
sweep.

## Logging

Each branch save (or skip) appends one JSON line to
`data/auto-remember/runs.jsonl` with a `branch` tag:

```
{"status":"pass","branch":"feedback","data_class":"feedback","entry_id":N,"deduped":false}
{"status":"pass","branch":"person","data_class":"current-state","entry_id":N,"deduped":false}
{"status":"pass","branch":"tech","data_class":"current-state","entry_id":N,"deduped":true}
{"status":"discard","branch":"person","reason":"name-unresolved","name":"Gary","count":"0"}
```

On failure (Ollama call, lucent write), the `$RUN_DIR/` breadcrumb is
retained with `ollama-request.json`, `ollama-response.json`,
`lucent-request-<branch>.json`, and `lucent-response-<branch>.json` so
the postmortem evidence is preserved. Success paths clean up the
breadcrumb.

## Open follow-ups

- **Context-augmented person query.** v1 ships naive
  `/graph/query?entity_name=X`. A smarter path (subagent emits JSON
  scope spec with typed edge constraints; hook walks the anchor to
  disambiguate names like "my brother David" vs "David from work")
  is deferred until measurement shows the naive false-drop rate is
  meaningful.
- **Multi-hop scope** for the smarter path is also deferred —
  single-hop is v1 when we revive it.
