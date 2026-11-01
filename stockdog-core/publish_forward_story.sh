#!/bin/bash
# IMPR-077 — Non-trading-day publish path for weekly/preview narratives.
#
# This wrapper publishes forward-content narratives (weekly OR preview) that are
# generated on NON-trading days (Sun/Mon), bypassing the daily-market guard in
# sync_vault.sh that prevents Sun/Mon publishes (line ~215-218: gate on
# raw/stockdog/daily-market/$DATE existence).
#
# The generators (generate_weekly_story.py, generate_preview_story.py) are:
# - GATED: LLM called only when gates pass (snapshot exists, idempotent not-already-ok, etc.)
# - Idempotent: If status=="ok" already exists for the date, NO LLM call
# - Always exit 0: failures (gate skip, LLM error, validation fail) → status:skipped + exit 0
#
# This wrapper:
# 1. Runs the generator (which is gated + idempotent + $0-exit)
# 2. Parses narrative.json status / report_date (Python needed for JSON parsing)
# 3. If status != "ok" OR report_date != $DATE: skipped → log "nothing to publish" + exit 0
# 4. If status == "ok" for $DATE: render + stage + commit + push (same as sync_vault's publish_stories)
# 5. Always exit 0 on the publish path (failed push → log but don't hard-fail)
#
# Dashboard rebuild is handled SEPARATELY by the 5-min build_all.sh cron,
# which reads narrative.json; do NOT call build_all from here.
#
# Usage:
#   publish_forward_story.sh <generator_filename> [<date>]
#   generator_filename:  e.g., "generate_preview_story.py" or "generate_weekly_story.py"
#   date:                YYYY-MM-DD (default: today via `date +%F`)

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VAULT_DIR="$DIR/../../skyler"
DATE="${2:-$(date +%Y-%m-%d)}"
GENERATOR="${1}"

# Load shared .env
set -a; source "$DIR/../.env"; set +a

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=$1" \
        -d "parse_mode=Markdown" > /dev/null
}

# ---- Step 1: Verify vault dir exists
cd "$VAULT_DIR" || {
    send_telegram "⚠️ Forward story publish failed: skyler directory not found at $VAULT_DIR"
    exit 1
}

# ---- Step 2: git pull to ensure freshness
echo "[publish_forward_story.sh] pulling vault repo ($VAULT_DIR)..."
git pull || {
    echo "[publish_forward_story.sh] git pull failed"
    exit 1
}

# ---- Step 3: Run the generator in docker (gated + idempotent + $0-exit)
echo "[publish_forward_story.sh] running $GENERATOR for $DATE..."
( cd "$DIR" && /usr/bin/docker compose run --rm stockdog python "$GENERATOR" /notes "$DATE" ) || {
    echo "[publish_forward_story.sh] generator docker invocation failed (exit $?)"
    exit 1
}

# ---- Step 4: Parse narrative.json to check status & report_date
echo "[publish_forward_story.sh] parsing narrative.json..."
NARRATIVE_FILE="$VAULT_DIR/raw/stockdog/narrative/narrative.json"

# Use python3 to parse JSON (more portable than jq)
PARSE_RESULT=$(python3 << 'ENDPYTHON'
import sys
import json
from pathlib import Path

try:
    path = Path("/home/ubuntu/service/skyler/raw/stockdog/narrative/narrative.json")
    if not path.is_file():
        print("FILE_NOT_FOUND")
        sys.exit(0)

    data = json.loads(path.read_text(encoding="utf-8"))
    status = data.get("status", "")
    report_date = data.get("report_date", "")
    print(f"{status}|{report_date}")
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(0)
ENDPYTHON
)

echo "[publish_forward_story.sh] narrative.json parse: $PARSE_RESULT"

# Parse result: "status|report_date" or "ERROR:..."
if [[ "$PARSE_RESULT" == "FILE_NOT_FOUND" ]]; then
    echo "[publish_forward_story.sh] narrative.json does not exist — skipped (no-op)"
    exit 0
elif [[ "$PARSE_RESULT" == "PARSE_ERROR:"* ]]; then
    echo "[publish_forward_story.sh] failed to parse narrative.json: $PARSE_RESULT — skipped (no-op)"
    exit 0
fi

# Extract status and report_date from "status|report_date"
STATUS=$(echo "$PARSE_RESULT" | cut -d'|' -f1)
REPORT_DATE=$(echo "$PARSE_RESULT" | cut -d'|' -f2)

echo "[publish_forward_story.sh] status=$STATUS, report_date=$REPORT_DATE, target_date=$DATE"

# ---- Step 5: Gate on status=="ok" and report_date==$DATE
if [[ "$STATUS" != "ok" ]] || [[ "$REPORT_DATE" != "$DATE" ]]; then
    echo "[publish_forward_story.sh] skipped (gate/no-op) — nothing to publish (status=$STATUS or report_date=$REPORT_DATE != $DATE)"
    exit 0
fi

# ---- Step 6: Render the stories pages (stdlib-only, no LLM)
echo "[publish_forward_story.sh] rendering stories index + detail pages..."
python3 "$DIR/render_stories_index.py" "$VAULT_DIR" "$DATE" || {
    echo "[publish_forward_story.sh] render_stories_index.py failed"
    exit 1
}
python3 "$DIR/render_stories_detail.py" "$VAULT_DIR" || {
    echo "[publish_forward_story.sh] render_stories_detail.py failed"
    exit 1
}

# ---- Step 7: Stage tightly scoped (NEVER -A)
echo "[publish_forward_story.sh] staging raw/stockdog/narrative/ + 10_Public/daily-stories/..."
git add "raw/stockdog/narrative/" "10_Public/daily-stories/"

# ---- Step 8: Check if anything changed, commit if needed
if git diff --cached --quiet; then
    echo "[publish_forward_story.sh] nothing new to push (graceful no-op)"
    exit 0
fi

git commit -m "Forward story $DATE ($GENERATOR)" || {
    echo "[publish_forward_story.sh] git commit failed"
    exit 1
}

# ---- Step 9: Push (always exit 0 on the publish path; failed push is logged but non-fatal)
echo "[publish_forward_story.sh] pushing to master..."
if git push origin master 2>&1; then
    # ---- Step 10: Wait until the page is actually live before notifying.
    # The blog is rebuilt by a separate 5-min build_all.sh cron (~2-3 min per build),
    # so notifying right after push sent a link that 404'd for 5-10 min (2026-11-01).
    STORY_URL="https://blog.seosungho.com/daily-stories/$DATE"
    WAIT_MAX_SEC="${STORY_WAIT_MAX_SEC:-900}"
    WAIT_STEP_SEC=30
    waited=0
    live=0
    code="000"
    while (( waited < WAIT_MAX_SEC )); do
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$STORY_URL" || true)
        if [[ "$code" == "200" ]]; then live=1; break; fi
        sleep "$WAIT_STEP_SEC"
        waited=$(( waited + WAIT_STEP_SEC ))
    done
    if (( live )); then
        echo "[publish_forward_story.sh] page live after ${waited}s: $STORY_URL"
        send_telegram "📤 *Forward story published*\nDate: $DATE\nGenerator: $GENERATOR\n🔗 $STORY_URL"
    else
        echo "[publish_forward_story.sh] page not live after ${WAIT_MAX_SEC}s (last HTTP $code): $STORY_URL"
        send_telegram "📤 *Forward story published* (사이트 반영 지연 — ${WAIT_MAX_SEC}s 내 미반영, HTTP $code)\nDate: $DATE\nGenerator: $GENERATOR\n🔗 $STORY_URL"
    fi
    echo "[publish_forward_story.sh] ✅ forward story published: $DATE ($GENERATOR)"
    exit 0
else
    send_telegram "⚠️ Forward story publish failed on git push. Check logs."
    echo "[publish_forward_story.sh] ⚠️ git push failed (non-fatal)"
    exit 0  # Always exit 0 on the publish path
fi
