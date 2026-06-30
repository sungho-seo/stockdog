#!/bin/bash
# Pushes session_summaries/stockdog.md to the skyler vault on GitHub.

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VAULT_DIR="$DIR/../../skyler"
DATE=$(date +%Y-%m-%d)

set -a; source "$DIR/../.env"; set +a

cd "$VAULT_DIR" || { echo "❌ skyler not found at $VAULT_DIR"; exit 1; }

mkdir -p "raw/stockdog/session_summaries"
cp "$DIR/../session_summaries/stockdog.md" "raw/stockdog/session_summaries/stockdog.md" || {
    echo "❌ session_summaries/stockdog.md not found"
    exit 1
}

git add "raw/stockdog/session_summaries/"

if git diff --cached --quiet; then
    echo "Nothing new to sync."
    exit 0
fi

git commit -m "Summary sync $DATE"

if git push origin master 2>&1; then
    echo "✅ Summary synced: $DATE"
else
    echo "❌ Push failed."
    exit 1
fi
