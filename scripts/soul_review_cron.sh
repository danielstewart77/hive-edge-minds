#!/usr/bin/env bash
# Once a day, ask Skippy to look at who he is.
#
# The sub-mind's flags only ever point at the conversation happening now, so
# they can add and they can reword, but they can never notice that a line
# written in May stopped being true in July. Nothing in a live conversation
# flags a stale soul line. This is the pass that can.
#
# It dispatches a real turn through hive-comms rather than running a tool:
# the judgment has to be Skippy's at full size, which is the entire point of
# requirement 14. No Ollama anywhere in this path.
#
# The prompt is worded to invoke review-identity-proposal, the same skill the
# flag path invokes. One skill, three entry points.
set -uo pipefail

SKIPPY_ROOT="${HIVE_PROJECT_DIR:-/home/daniel/Storage/hive-edge-mind-skippy}"
LOG="$SKIPPY_ROOT/data/auto-remember/soul_review_cron.log"
mkdir -p "$(dirname "$LOG")"

set -a; [ -f "$SKIPPY_ROOT/.env" ] && . "$SKIPPY_ROOT/.env"; set +a

PROMPT='Review this identity proposal: your own soul, as a whole. Nothing has been flagged — this is the daily pass. Read your soul from the graph, read what you have been doing lately, and decide whether anything in it should be reworded or removed for having gone stale. Adding is allowed but is not the point of this pass; noticing what is no longer true is.'

stamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

if [ "${1:-}" = "--dry-run" ]; then
    # What would be sent, and where. Exists so the wiring is testable
    # without dispatching a turn to a live mind at test time.
    jq -nc --arg url "${COMMS_URL:-}" --arg mind "${MIND_ID:-}" \
           --arg owner "scheduler" --arg prompt "$PROMPT" \
        '{comms_url:$url, mind_id:$mind, owner_type:$owner, prompt:$prompt}'
    exit 0
fi

AUTH="Authorization: Bearer ${COMMS_BEARER_TOKEN:-}"

SESSION=$(curl -sS -m 30 -X POST "${COMMS_URL:?}/sessions" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "$(jq -nc --arg mid "${MIND_ID:?}" \
        '{owner_type:"scheduler", owner_ref:"soul-review", client_ref:"soul-review-cron", mind_id:$mid}')" 2>&1)

SID=$(printf '%s' "$SESSION" | jq -r '.id // .session_id // ""' 2>/dev/null)
if [ -z "$SID" ]; then
    echo "[$(stamp)] could not open a session: $SESSION" >> "$LOG"
    exit 0
fi

RESP=$(curl -sS -m 900 -X POST "${COMMS_URL}/sessions/$SID/message" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "$(jq -nc --arg m "$PROMPT" '{message:$m}')" 2>&1)

echo "[$(stamp)] session=$SID dispatched; reply=$(printf '%s' "$RESP" | head -c 400)" >> "$LOG"
exit 0
