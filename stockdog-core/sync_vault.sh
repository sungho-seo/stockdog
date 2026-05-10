#!/bin/bash
# Pushes daily Markdown reports to the skyler vault on GitHub.
# Run after main.py completes. Media files (images) are excluded.

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VAULT_DIR="$DIR/../skyler"
DATE=$(date +%Y-%m-%d)

# Load API keys and tokens from shared repo-root .env
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

# Stage all report files, then unstage media/ (images)
git add daily-market/
git restore --staged -- "daily-market/*/media/" 2>/dev/null || true

# Skip if nothing changed
if git diff --cached --quiet; then
    echo "Vault sync: nothing new to push."
    exit 0
fi

git commit -m "Daily report $DATE"

# Push using GITHUB_PAT for authentication (works for both public and private repo)
if git push "https://${GITHUB_PAT}@github.com/sungho-seo/skyler.git" main 2>&1; then
    send_telegram "📤 *Vault synced*\n\`daily-market/$DATE\` → GitHub"
    echo "✅ Vault synced: $DATE"
else
    send_telegram "⚠️ Vault sync failed on git push. Check cron_stockdog.log."
    echo "❌ Vault push failed."
    exit 1
fi
