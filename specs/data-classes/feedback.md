# Data Class: feedback

## Description
User-supplied input that should shape the mind's future behavior:
preferences, corrections, judgments about what worked or didn't,
behavioral rules.

Covers:
- Stable preferences ("I prefer Yorkshire Gold for breakfast tea").
- Workflow corrections ("don't restart a long-running service from a
  session that's currently using it").
- Validated approaches ("the bash hook pattern was right; do that").
- Pushback on the mind's behavior ("stop summarising every turn").

## Chunk shape
A feedback chunk is the rule itself, restated as guidance for the agent going forward — not a transcript of the turn.

- One short paragraph (1–3 sentences).
- Lead with the rule in imperative form, addressed to the agent ("Don't X." / "When Y, do Z.").
- Then a `**Why:**` line — the motivation the user gave (preference, prior incident, validated approach).
- Then a `**How to apply:**` line — when or where the rule kicks in.
- No transcript markers (`## User` / `## Assistant`). No quoting the user verbatim unless the wording itself matters.

## Actions
- save-vector

## Tier
- `contextual` (default) — written by classifier auto-capture.
- `standing` — written only via `/always-remember`. Loaded at every
  bootstrap. Skips both pruning paths below; removed only by user action.

Lucent enforces `tier=standing` requires `source=always-remember`.

## Pruning
For contextual-tier entries only.

1. **Contradiction-detection at capture** — before saving a new feedback
   chunk, similarity-search within `data_class=feedback` for the top-K
   closest existing entries. POST to `${HIVE_TOOLS_URL}/ollama/structured`
   with both statements and schema `{contradicts: bool, reason: string}`.
   If `contradicts: true`, delete the old entry before writing the new
   one.
2. **Decay-on-age** — `half_life_days: 90`, `delete_below_score: 0.02`.

- cadence: "0 4 * * *"
