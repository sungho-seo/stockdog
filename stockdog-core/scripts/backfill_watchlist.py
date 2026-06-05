"""IMPR-062: backfill watchlist price/volume history (container-side).

Idempotent / re-runnable: every write is keyed by trade_date via
m7_store.append_per_symbol (same-date records are replaced, not duplicated), so
re-running converges to the same state. Reuses the same per-symbol store the
live US pipeline writes to (CATEGORY="price"), so backfill + live capture share
one date axis (C3).

C1: history_cap_days=180 EVERYWHERE so the ~125-point 6-month backfill is not
clipped by m7_store's default 90.

Source: yfinance .history(period="6mo") per ticker.

Usage (inside the stockdog container):
    python scripts/backfill_watchlist.py
"""
import os
import sys
import logging

# Allow `python scripts/backfill_watchlist.py` from stockdog-core root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import m7_store  # noqa: E402
from utils.vault_reader import read_watchlist_items  # noqa: E402
from utils.watchlist_store import (  # noqa: E402
    CATEGORY,
    HISTORY_CAP_DAYS,
    stage_watchlist_snapshot,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

WL_FILE = os.getenv("WATCHLIST_FILE", "/notes/_system/watchlist.md")
WL_DIR = "/notes/raw/stockdog/watchlist"


def backfill_ticker(tk, name, type_):
    """Backfill one ticker's 6mo history. Returns (n_points, span) or (0, '—')."""
    import yfinance as yf
    hist = yf.Ticker(tk).history(period="6mo")
    if hist is None or hist.empty:
        print(f"  {tk:<6} no data, skipping")
        return 0, "—"

    closes = list(hist["Close"])
    vols = list(hist["Volume"]) if "Volume" in hist.columns else [None] * len(closes)
    dates = list(hist.index)

    n = 0
    first = last = None
    prev_close = None
    last_rec = None
    for i in range(len(closes)):
        try:
            d = dates[i].strftime("%Y-%m-%d")
        except Exception:
            continue
        try:
            close = float(closes[i])
        except (TypeError, ValueError):
            continue
        if close != close:  # NaN (yfinance emits NaN for some rows)
            continue
        close = round(close, 2)
        if prev_close is not None and prev_close not in (0, None):
            change_pct = round((close - prev_close) / prev_close * 100, 2)
        else:
            change_pct = None
        try:
            vraw = float(vols[i]) if vols[i] is not None else None
            volume = int(vraw) if (vraw is not None and vraw == vraw) else None
        except (TypeError, ValueError):
            volume = None

        rec = {
            "date": d,
            "close": close,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume": volume,
            "name": name,
            "type": type_,
        }
        m7_store.append_per_symbol(
            WL_DIR, CATEGORY, tk, rec, history_cap_days=HISTORY_CAP_DAYS
        )
        last_rec = rec
        first = first or d
        last = d
        prev_close = close
        n += 1

    if last_rec is not None:
        m7_store.write_per_symbol_latest(WL_DIR, CATEGORY, tk, last_rec)
    print(f"  {tk:<6} {n:>4} pts  [{first}..{last}]")
    return n, f"{first}..{last}"


def main():
    items = read_watchlist_items(WL_FILE, types=("STOCK", "ETF", "INDEX_US"))
    print(f"[backfill] watchlist: {len(items)} tickers from {WL_FILE}")
    total_ok = 0
    for item in items:
        tk = item["ticker"]
        try:
            n, _ = backfill_ticker(tk, item.get("name"), item.get("type"))
            if n:
                total_ok += 1
        except Exception as e:
            print(f"  {tk:<6} FAILED, skipping: {e}")
    stage_watchlist_snapshot(WL_DIR)
    print(f"[backfill] done. {total_ok}/{len(items)} tickers backfilled.")


if __name__ == "__main__":
    main()
