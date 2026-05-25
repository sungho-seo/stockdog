#!/bin/bash
# Pushes daily Markdown reports to the skyler vault on GitHub.
# Pipeline writes directly to raw/stockdog/daily-market/YYYY-MM-DD/ via Docker volume.

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VAULT_DIR="$DIR/../../skyler"
DATE=$(date +%Y-%m-%d)

# Load shared .env
set -a; source "$DIR/../.env"; set +a

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=$1" \
        -d "parse_mode=Markdown" > /dev/null
}

cd "$VAULT_DIR" || {
    send_telegram "⚠️ Vault sync failed: skyler directory not found at $VAULT_DIR"
    exit 1
}

# Verify today's report exists
if [ ! -d "raw/stockdog/daily-market/$DATE" ]; then
    echo "Report not found: raw/stockdog/daily-market/$DATE"
    send_telegram "⚠️ Vault sync failed: no report found for $DATE"
    exit 1
fi

# Stage daily report
git add "raw/stockdog/daily-market/$DATE/"

# Stage M7 tracker output (IMPR-044). m7_job.py runs separately and writes:
#   raw/stockdog/m7/insider/$DATE.json + raw/stockdog/m7/short/$DATE.json
#   raw/stockdog/m7/<TICKER>/{insider,short}_{history,latest}.json (×7)
# Missing dirs are harmless — git add silently skips them when m7_job hasn't
# run yet (e.g., emergency disable via config m7.enabled=false).
git add "raw/stockdog/m7/" 2>/dev/null || true

# Skip if nothing changed
if git diff --cached --quiet; then
    echo "Vault sync: nothing new to push."
    exit 0
fi

git commit -m "Daily report $DATE"

# Push to master branch using GITHUB_PAT
if git push "https://${GITHUB_PAT}@github.com/sungho-seo/skyler.git" master 2>&1; then
    send_telegram "📤 *Vault synced*\n\`raw/stockdog/daily-market/$DATE\` → GitHub"
    echo "✅ Vault synced: $DATE"

    # Sprint 1C — vault-web 자동 빌드 트리거 (post-hook).
    # - D-1: push 성공 분기 안에서만 발화 (push 실패 시는 의미 없음).
    # - quality R-3: setsid로 새 세션 분리 → SIGHUP 차단. disown 불필요.
    # - { ... } || true: 빌드 트리거 실패가 stockdog cron 종료 코드 오염 안 시키게.
    # - 호출 그래프 단방향: build_all.sh는 stockdog/sync_vault.sh를 호출하지 않음 → 무한 루프 불가.
    {
        VAULT_WEB_BUILD="/home/ubuntu/service/agents/vault-web/scripts/build_all.sh"
        VAULT_WEB_LOG="/home/ubuntu/service/agents/vault-web/logs/posthook_$(date +%Y%m%d).log"
        if [[ -f "${VAULT_WEB_BUILD}" ]]; then
            echo "[sync_vault.sh] triggering vault-web build_all.sh (post-hook)" >&2
            setsid bash "${VAULT_WEB_BUILD}" \
                </dev/null \
                >>"${VAULT_WEB_LOG}" 2>&1 \
                &
        fi
    } || true
else
    send_telegram "⚠️ Vault sync failed on git push. Check cron_stockdog.log."
    echo "❌ Vault push failed."
    exit 1
fi
