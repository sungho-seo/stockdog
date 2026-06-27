#!/usr/bin/env python3
"""
NVDL 3:1 split backfill (2026-06-26).
Manual-only script. Corrects both price_history.json and price_latest.json
for split-contaminated prices (close and prev_close). Idempotent.
"""
import json
import os
import sys
from pathlib import Path

SPLIT_DATE = "2026-06-26"
SPLIT_RATIO = 3
# When running inside docker, the vault is mounted at /notes
# When running on host, use ~/service/skyler
if os.path.exists("/notes"):
    VAULT_ROOT = "/notes"
else:
    VAULT_ROOT = os.path.expanduser("~/service/skyler")
NVDL_WATCHLIST_DIR = os.path.join(VAULT_ROOT, "raw/stockdog/watchlist/NVDL")
PRICE_HISTORY_FILE = os.path.join(NVDL_WATCHLIST_DIR, "price_history.json")
PRICE_LATEST_FILE = os.path.join(NVDL_WATCHLIST_DIR, "price_latest.json")

# Pre-split range threshold: NVDL traded ~$80-99 pre-split, ~$27-33 post-split.
# Use 45 as the boundary: >45 is pre-split, <=45 is post-split.
PRE_SPLIT_THRESHOLD = 45


def _atomic_write_json(path: str, payload) -> None:
    """Atomic write with tmp + os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def needs_adjustment(close, prev_close) -> bool:
    """
    Idempotency check based on VALUE RANGE.
    Returns True if ANY field needs adjustment (close or prev_close in pre-split range).
    Returns False if row is fully adjusted (both close and prev_close in post-split range or None).

    Pre-split range: > 45
    Post-split range: <= 45
    """
    close_needs_adjustment = close > PRE_SPLIT_THRESHOLD
    prev_close_needs_adjustment = (prev_close is not None) and (prev_close > PRE_SPLIT_THRESHOLD)
    return close_needs_adjustment or prev_close_needs_adjustment


def backfill_price_history():
    """Fix price_history.json: divide pre-split prices by 3 based on value range."""
    print(f"[backfill] reading {PRICE_HISTORY_FILE}")
    with open(PRICE_HISTORY_FILE) as f:
        history = json.load(f)

    adjusted_count = 0
    skipped_count = 0

    for i, row in enumerate(history):
        date = row["date"]
        close = row["close"]
        prev_close = row.get("prev_close")

        # Idempotency: skip if row is fully adjusted (no value > PRE_SPLIT_THRESHOLD)
        if not needs_adjustment(close, prev_close):
            skipped_count += 1
            continue

        if date < SPLIT_DATE:
            # Pre-split: divide close and/or prev_close if they're in pre-split range
            if close > PRE_SPLIT_THRESHOLD:
                row["close"] = round(close / SPLIT_RATIO, 2)
            if prev_close is not None and prev_close > PRE_SPLIT_THRESHOLD:
                row["prev_close"] = round(prev_close / SPLIT_RATIO, 2)
            # change_pct unchanged (it's a ratio, survives the division)
            adjusted_count += 1

        elif date == SPLIT_DATE:
            # Split day: divide prev_close by 3 if needed, keep close, recompute change_pct
            if prev_close is not None and prev_close > PRE_SPLIT_THRESHOLD:
                new_prev_close = round(prev_close / SPLIT_RATIO, 3)
                row["prev_close"] = new_prev_close
                # Recompute change_pct if prev_close != 0
                if new_prev_close != 0:
                    row["change_pct"] = round((close - new_prev_close) / new_prev_close * 100, 2)
            adjusted_count += 1

        # else: date > SPLIT_DATE, leave untouched

    print(f"  adjusted {adjusted_count} rows, skipped {skipped_count} (idempotent)")

    # Atomic write
    _atomic_write_json(PRICE_HISTORY_FILE, history)
    print(f"  wrote {PRICE_HISTORY_FILE}")
    return adjusted_count, skipped_count


def backfill_price_latest():
    """Fix price_latest.json: apply split logic based on value range."""
    print(f"[backfill] reading {PRICE_LATEST_FILE}")
    with open(PRICE_LATEST_FILE) as f:
        latest = json.load(f)

    close = latest["close"]
    prev_close = latest.get("prev_close")

    # Idempotency: skip if row is fully adjusted
    if not needs_adjustment(close, prev_close):
        print(f"  skipped (idempotent: close={close}, prev_close={prev_close} already post-split)")
        return 0, 1

    date = latest["date"]

    if date == SPLIT_DATE:
        # Split day: divide prev_close by 3 if needed, keep close, recompute change_pct
        if prev_close is not None and prev_close > PRE_SPLIT_THRESHOLD:
            new_prev_close = round(prev_close / SPLIT_RATIO, 3)
            latest["prev_close"] = new_prev_close
            # Recompute change_pct if new_prev_close != 0
            if new_prev_close != 0:
                latest["change_pct"] = round((close - new_prev_close) / new_prev_close * 100, 2)

        print(f"  adjusted 1 row")
        _atomic_write_json(PRICE_LATEST_FILE, latest)
        print(f"  wrote {PRICE_LATEST_FILE}")
        return 1, 0
    else:
        print(f"  skipped: date {date} != {SPLIT_DATE}")
        return 0, 1


def main():
    print("=== NVDL 3:1 Split Backfill ===")
    print(f"Split date: {SPLIT_DATE}, ratio: {SPLIT_RATIO}:1")
    print()

    try:
        hist_adj, hist_skip = backfill_price_history()
        print()
        latest_adj, latest_skip = backfill_price_latest()

        print()
        print(f"=== Summary ===")
        print(f"price_history.json: {hist_adj} adjusted, {hist_skip} skipped")
        print(f"price_latest.json: {latest_adj} adjusted, {latest_skip} skipped")
        print()

        # Verification
        with open(PRICE_HISTORY_FILE) as f:
            history = json.load(f)
        split_row = [r for r in history if r["date"] == SPLIT_DATE]
        if split_row:
            sr = split_row[0]
            print(f"Verification (split day row):")
            print(f"  close={sr['close']}, prev_close={sr['prev_close']}, change_pct={sr['change_pct']}%")
            expected_change = (sr['close'] - sr['prev_close']) / sr['prev_close'] * 100
            print(f"  computed change = {expected_change:.2f}%")

        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
