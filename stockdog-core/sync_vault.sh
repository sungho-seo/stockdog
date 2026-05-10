#!/bin/bash
# Pushes daily Markdown reports to the skyler vault on GitHub.
# Local structure (daily-market/) is preserved as-is on the server.
# GitHub target path: raw/stockdog/daily-market/YYYY-MM-DD/

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VAULT_DIR="$DIR/../../skyler"
DATE=$(date +%Y-%m-%d)

# Local source / GitHub target
SRC="$VAULT_DIR/daily-market/$DATE"
DEST="$VAULT_DIR/raw/stockdog/daily-market/$DATE"

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

# Copy today's .md files to the GitHub target path (exclude media/)
if [ ! -d "$SRC" ]; then
    echo "Source directory not found: $SRC"
    send_telegram "⚠️ Vault sync failed: no report found for $DATE"
    exit 1
fi

mkdir -p "$DEST"
cp "$SRC"/*.md "$DEST/" 2>/dev/null

# Copy session summary
mkdir -p "raw/stockdog/session_summaries"
cp "$DIR/../session_summaries/stockdog.md" "raw/stockdog/session_summaries/stockdog.md" 2>/dev/null

# Stage target paths
git add "raw/stockdog/daily-market/$DATE/" "raw/stockdog/session_summaries/"

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
else
    send_telegram "⚠️ Vault sync failed on git push. Check cron_stockdog.log."
    echo "❌ Vault push failed."
    exit 1
fi
