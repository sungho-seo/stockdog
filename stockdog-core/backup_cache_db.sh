#!/bin/bash
# Daily backup of stockdog cache SQLite DBs (metrics_history.db, etc.).
#
# Why: cache/ is gitignored + lives on a persistent host volume, so the
# git-HEAD recovery path canNOT restore it. The 2026-06-27 `main.py --sample`
# clobber wiped market_metrics down to 3 rows and went unnoticed until the
# 30-day dashboard chart collapsed. This gives us a dated, rotated safety net.
#
# Safety: a DB with a market_metrics table is only backed up when it holds
# >= MIN_ROWS rows — so a *wiped* DB never overwrites/rotates out good copies.
# Host-run (cron), reads the root-owned (0644) DB fine, no docker, no sudo, $0.
set -euo pipefail

SRC_DIR="/home/ubuntu/service/stockdog/stockdog-core/cache"
DEST_DIR="/home/ubuntu/service/backups/metrics_db"
RETAIN=30           # keep newest N dated copies per db name
MIN_ROWS=10         # min market_metrics rows required to accept a backup
STAMP="$(date +%Y-%m-%d)"

mkdir -p "$DEST_DIR"
shopt -s nullglob

for db in "$SRC_DIR"/*.db; do
  [ -s "$db" ] || continue            # skip empty/zero-byte
  name="$(basename "$db" .db)"

  # Guard: if this DB has a market_metrics table, require >= MIN_ROWS rows.
  # If the table is absent, skip the guard (back up other DBs unconditionally).
  ok="$(python3 - "$db" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    has = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_metrics'"
    ).fetchone()
    if not has:
        print("OK"); sys.exit(0)          # no guarded table → always back up
    n = c.execute("SELECT COUNT(*) FROM market_metrics").fetchone()[0]
    print("OK" if n >= 10 else f"SKIP:{n}")
except Exception as e:
    print(f"SKIP:err:{e}")
PY
)"

  if [[ "$ok" != "OK" ]]; then
    echo "[$(date +%H:%M)] skip ${name}.db — guard (${ok})"
    continue
  fi

  cp -p "$db" "$DEST_DIR/${name}.${STAMP}.db"
  echo "[$(date +%H:%M)] backed up ${name}.db -> ${name}.${STAMP}.db"

  # Rotation: keep newest RETAIN copies for this db name, delete older.
  ls -1t "$DEST_DIR/${name}."*.db 2>/dev/null | tail -n +"$((RETAIN+1))" | xargs -r rm -f
done
