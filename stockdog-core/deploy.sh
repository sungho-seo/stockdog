#!/bin/bash

echo "🐾 Setting up StockDog Cron Jobs for Ubuntu..."

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd $DIR

# Wire the pre-commit hook for the undefined-name gate (F821/F822).
# This is a local git config (not committed); re-run deploy.sh on a fresh server to restore it.
git -C "$DIR" config core.hooksPath githooks
echo "✅ git core.hooksPath set to githooks (pre-commit undefined-name gate active)."

# ── Cron Job 1: Main Pipeline ─────────────────────────────────────────
# 17:00 KST = 08:00 UTC — vault pull → pipeline → vault push
CRON_MAIN="0 17 * * * cd $DIR && git -C $DIR/../../skyler pull >> $DIR/cron_stockdog.log 2>&1; /usr/bin/docker compose run --rm stockdog python main.py >> $DIR/cron_stockdog.log 2>&1 && bash $DIR/sync_vault.sh >> $DIR/cron_stockdog.log 2>&1"

# ── Cron Job 2: Fear & Greed at US Market Open ────────────────────────
# 22:30 KST = 13:30 UTC = 09:30 ET (Mon-Fri)
CRON_FG="30 13 * * 1-5 cd $DIR && /usr/bin/docker compose run --rm stockdog python fear_greed_job.py >> $DIR/cron_fear_greed.log 2>&1"

# ── Cron Job 3: Vault pull — keep oracle vault in sync ────────────────
# Every 6 hours (picks up edits made in local Obsidian)
CRON_VAULT_PULL="0 */6 * * * cd $DIR/../../skyler && git pull >> $DIR/cron_vault_sync.log 2>&1"

# ── Install: always replace main pipeline and vault pull ──────────────
CURRENT_CRON=$(crontab -l 2>/dev/null | grep -v "cron_stockdog.log" | grep -v "cron_vault_sync.log" || true)

CURRENT_CRON="${CURRENT_CRON}
${CRON_MAIN}"
echo "✅ Main pipeline cron set (17:00 KST = 08:00 UTC, vault pull + push)."

if crontab -l 2>/dev/null | grep -Fq "cron_fear_greed.log"; then
    echo "⚠️  Fear & Greed cron already exists, skipping."
else
    CURRENT_CRON="${CURRENT_CRON}
${CRON_FG}"
    echo "✅ Fear & Greed cron added (22:30 KST Mon-Fri)."
fi

CURRENT_CRON="${CURRENT_CRON}
${CRON_VAULT_PULL}"
echo "✅ Vault pull cron set (every 6 hours)."

echo "$CURRENT_CRON" | crontab -

echo ""
echo "📋 Current crontab:"
crontab -l
echo ""
echo "To manually trigger:"
echo "  Main pipeline: docker compose run --rm stockdog python main.py"
echo "  Vault sync:    bash $DIR/sync_vault.sh"
echo "  F&G:           docker compose run --rm stockdog python fear_greed_job.py"
