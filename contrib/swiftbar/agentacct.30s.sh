#!/bin/bash
# <xbar.title>agentacct</xbar.title>
# <xbar.version>v0.1</xbar.version>
# <xbar.desc>Today's coding-agent cost + provider limits from the local agentacct daemon.</xbar.desc>
# <xbar.dependencies>jq,curl</xbar.dependencies>
#
# SwiftBar/xbar plugin: drop into your plugin folder. Reads the daemon's
# discovery file (port + per-boot bearer token, 0600) — no configuration.
# Doubles as the reference /v1/glance client.
set -uo pipefail

STORE="${AGENTACCT_STORE_DIR:-$HOME/.local/state/agentacct/state}"
DISCOVERY="$STORE/local-api.json"

if ! command -v jq >/dev/null 2>&1; then
  echo "⏺ agentacct"
  echo "---"
  echo "jq is required (brew install jq)"
  exit 0
fi

if [ ! -r "$DISCOVERY" ]; then
  echo "⏺ ∅"
  echo "---"
  echo "agentacct daemon not running"
  echo "Start it: agentacct start | font=Menlo"
  exit 0
fi

PORT=$(jq -r '.port' "$DISCOVERY")
TOKEN=$(jq -r '.token' "$DISCOVERY")

GLANCE=$(curl -s -m 10 -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:${PORT}/v1/glance") || GLANCE=""
if [ -z "$GLANCE" ] || ! echo "$GLANCE" | jq -e '.schema == "agentacct.glance.v1"' >/dev/null 2>&1; then
  echo "⏺ ∅"
  echo "---"
  echo "daemon not reachable on port ${PORT} (or incompatible)"
  echo "Start it: agentacct start | font=Menlo"
  exit 0
fi

# Menu bar: today's cost — complete "$", partial "~$", nothing priced "—".
TODAY_COST=$(echo "$GLANCE" | jq -r '
  (.usage.windows[] | select(.label == "today") | .totals) as $t |
  if ($t.cost_complete == true and ($t.estimated_cost_usd != null)) then "$" + ($t.estimated_cost_usd * 100 | round / 100 | tostring)
  elif ($t.known_additive_cost_usd != null) then "~$" + ($t.known_additive_cost_usd * 100 | round / 100 | tostring)
  else "—" end')
echo "⏺ ${TODAY_COST}"
echo "---"

echo "$GLANCE" | jq -r '
  "Usage",
  (.usage.windows[] | select(.label != "all time") |
    "\(.label): \(if .totals.fresh_tokens != null then (.totals.fresh_tokens | tostring) else "—" end) tok · " +
    (.totals as $t |
      if ($t.cost_complete == true and ($t.estimated_cost_usd != null)) then "$" + ($t.estimated_cost_usd * 100 | round / 100 | tostring)
      elif ($t.known_additive_cost_usd != null) then "~$" + ($t.known_additive_cost_usd * 100 | round / 100 | tostring)
      else "—" end) + " | font=Menlo")'

echo "---"
echo "$GLANCE" | jq -r '
  "Limits",
  (.limits[] | . as $l | ($l.windows // [])[] | select(.used_percent != null) |
    "\($l.client // "?") \(.kind // ""): \(.used_percent | round)%\(if $l.stale == true then " (stale)" else "" end) | font=Menlo")'

echo "---"
echo "$GLANCE" | jq -r '
  "Recent sessions",
  (.recent_sessions[:5][] |
    (if .status == "blocked" then "⚠" elif .status == "in_progress" then "▶"
     elif .status == "completed" then "✓" elif .status == "handed_off" then "↗" else "·" end) + " " +
    (.title // (.client + " · " + (.session_id[:8]))) +
    (if .plan_pct != null then (if (.plan_pct > 0 and .plan_pct < 0.1) then " ≈<0.1%" else " ≈" + ((.plan_pct * 10 | round) / 10 | tostring) + "%" end) else "" end) +
    " | font=Menlo trim=false")'
