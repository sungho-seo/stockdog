#!/bin/bash

echo "🐾 Setting up StockDog Cron Jobs for Ubuntu..."

# Get the absolute path of the directory containing this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd $DIR

# ── Cron Job 1: Main Pipeline ──────────────────────────────────────
# Run daily at 02:00 UTC (= 11:00 KST) — after US after-hours market close
CRON_MAIN="0 2 * * * cd $DIR && /usr/bin/docker compose run --rm stockdog python main.py >> $DIR/cron_stockdog.log 2>&1 && bash $DIR/sync_vault.sh >> $DIR/cron_stockdog.log 2>&1"

# ── Cron Job 2: Fear & Greed at US Market Open ────────────────────
# Run Mon-Fri at 13:30 UTC (= 09:30 ET = 22:30 KST)
CRON_FG="30 13 * * 1-5 cd $DIR && /usr/bin/docker compose run --rm stockdog python fear_greed_job.py >> $DIR/cron_fear_greed.log 2>&1"

# Install cron jobs (idempotent)
CURRENT_CRON=$(crontab -l 2>/dev/null || echo "")

if echo "$CURRENT_CRON" | grep -Fq "cron_stockdog.log"; then
    echo "⚠️  Main pipeline cron already exists."
else
    CURRENT_CRON="$CURRENT_CRON
$CRON_MAIN"
    echo "✅ Main pipeline cron added (daily 02:00 UTC + vault sync)."
fi

if echo "$CURRENT_CRON" | grep -Fq "cron_fear_greed.log"; then
    echo "⚠️  Fear & Greed cron already exists."
else
    CURRENT_CRON="$CURRENT_CRON
$CRON_FG"
    echo "✅ Fear & Greed cron added (Mon-Fri 13:30 UTC = 09:30 ET)."
fi

echo "$CURRENT_CRON" | crontab -

echo ""
echo "📋 Current crontab:"
crontab -l
echo ""
echo "To manually trigger:"
echo "  Main:   docker compose run --rm stockdog python main.py"
echo "  F&G:    docker compose run --rm stockdog python fear_greed_job.py"
