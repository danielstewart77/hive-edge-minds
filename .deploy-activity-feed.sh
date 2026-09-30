#!/usr/bin/env bash
# Deploys the terminal-activity feed, then pokes every mind so the dashboard
# has live traffic to render. Detached on a timer because the skippy restart
# kills the conversation that scheduled it.
set -uo pipefail
LOG=/home/daniel/Storage/hive-edge-mind-skippy/.deploy-activity-feed.log
exec >>"$LOG" 2>&1
echo "=== $(date -Is) deploy start ==="

set -a; . /home/daniel/Storage/hive-edge-mind-skippy/.env; set +a

echo "--- rebuilding comms ---"
cd /home/daniel/Storage/Dev/hive_mind && docker compose up -d --build comms

code=""
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $COMMS_BEARER_TOKEN" http://127.0.0.1:8426/health || true)
  [ "$code" = "200" ] && { echo "comms healthy after ${i}s"; break; }
  sleep 1
done
[ "$code" = "200" ] || { echo "ABORT: comms did not come back (last=$code); skippy left running"; exit 1; }

echo "--- pty-activity route present? ---"
curl -s -o /dev/null -w 'pty-activity unauthenticated -> %{http_code} (401/403 = route exists and is guarded)\n' \
  -X POST http://127.0.0.1:8426/sessions/probe/pty-activity \
  -H 'Content-Type: application/json' -d '{"blocks":[]}'

echo "--- restarting skippy.service ---"
sudo systemctl restart skippy.service
code=""
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8421/health || true)
  [ "$code" = "200" ] && { echo "skippy healthy after ${i}s"; break; }
  sleep 1
done
echo "skippy final health: $code"
systemctl is-active skippy.service

echo "--- poking every mind ---"
sleep 5
for m in ada bob bilby nagatha mordecai hex arnold; do
  cid="ctxcheck-$(date +%s)-$m"
  out=$(curl -s -X POST http://127.0.0.1:8426/broker/messages \
    -H "Authorization: Bearer $COMMS_BEARER_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"conversation_id\":\"$cid\",\"from_mind\":\"skippy\",\"to_mind\":\"$m\",\"content\":\"Skippy here. Roll call for a dashboard check — reply with one short sentence: who you are and what you are doing right now. Nothing else, no tools.\"}")
  echo "$m -> $out"
  sleep 2
done

echo "=== $(date -Is) deploy done ==="
