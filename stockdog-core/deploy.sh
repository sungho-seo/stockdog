#!/bin/bash

echo "🐾 Setting up StockDog Cron Job for Ubuntu..."

# Get the absolute path of the directory containing this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd $DIR

# Define the cron job: Run docker-compose up every day at 17:00 (5 PM server time)
CRON_JOB="0 17 * * * cd $DIR && /usr/bin/docker-compose up --build >> $DIR/cron_stockdog.log 2>&1"

# Check if cron job already exists, if not, add it
if crontab -l 2>/dev/null | grep -Fq "cron_stockdog.log"; then
    echo "⚠️ Cron job already exists!"
else
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "✅ Cron job added! StockDog will run daily at 17:00 server time."
fi

echo "To manually trigger a run now, execute: docker-compose up --build"
