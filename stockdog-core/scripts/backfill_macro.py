"""IMPR-061: backfill macro history into metrics_history.db (container-side).

Idempotent / re-runnable: every write is a column-scoped UPSERT keyed by date
(daily) or (series, obs_date) (monthly), so re-running converges to the same
state — it never duplicates and never clobbers other columns (M1-safe).

Sources:
  - FRED daily series (yields / spread / fed funds / broad dollar): last 120 days
  - FRED monthly series (CPI / Core CPI / PPI): last 24 months
  - USD/KRW via yfinance KRW=X: last 6 months

Usage (inside the stockdog container):
    python scripts/backfill_macro.py
"""

import os
import sys
import logging
from datetime import date, timedelta

# Allow `python scripts/backfill_macro.py` from stockdog-core root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.fred_macro import DAILY_SERIES, MONTHLY_SERIES, fetch_history  # noqa: E402
from utils.metrics_history import _conn, DB_PATH  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _span(pairs):
    """Return 'first..last' date span for a list of (date, value), or '—'."""
    if not pairs:
        return "—"
    return f"{pairs[0][0]}..{pairs[-1][0]}"


def backfill_daily(conn, today):
    start = (today - timedelta(days=120)).isoformat()
    print(f"[backfill] daily series since {start}")
    for col, series_id in DAILY_SERIES.items():
        pairs = fetch_history(series_id, start)
        for d, v in pairs:
            conn.execute("INSERT OR IGNORE INTO market_metrics (date) VALUES (?)", (d,))
            conn.execute(f"UPDATE market_metrics SET {col}=? WHERE date=?", (v, d))
        print(f"  {col:<10} ({series_id:<10}) {len(pairs):>4} obs  [{_span(pairs)}]")


def backfill_monthly(conn, today):
    # ~24 months back; use 740 days to comfortably cover 24 monthly observations.
    start = (today - timedelta(days=740)).isoformat()
    print(f"[backfill] monthly series since {start}")
    for series, series_id in MONTHLY_SERIES.items():
        pairs = fetch_history(series_id, start)
        for d, v in pairs:
            conn.execute(
                "INSERT OR REPLACE INTO macro_monthly (series, obs_date, level) VALUES (?,?,?)",
                (series, d, v)
            )
        print(f"  {series:<10} ({series_id:<10}) {len(pairs):>4} obs  [{_span(pairs)}]")


def backfill_usd_krw(conn):
    print("[backfill] USD/KRW (yfinance KRW=X, 6mo)")
    try:
        import yfinance as yf
        hist = yf.Ticker("KRW=X").history(period="6mo")
    except Exception as e:
        print(f"  USD/KRW fetch failed, skipping: {e}")
        return
    if hist is None or hist.empty:
        print("  USD/KRW: no data returned, skipping")
        return
    count = 0
    first = last = None
    for ts, row in hist.iterrows():
        d = ts.strftime("%Y-%m-%d")
        try:
            v = round(float(row["Close"]), 2)
        except (TypeError, ValueError, KeyError):
            continue
        conn.execute("INSERT OR IGNORE INTO market_metrics (date) VALUES (?)", (d,))
        conn.execute("UPDATE market_metrics SET usd_krw=? WHERE date=?", (v, d))
        first = first or d
        last = d
        count += 1
    print(f"  usd_krw    (KRW=X)     {count:>4} obs  [{first}..{last}]")


def main():
    today = date.today()
    if not os.getenv("FRED_API_KEY"):
        print("[backfill] WARNING: FRED_API_KEY not set — FRED series will be empty.")
    with _conn(DB_PATH) as conn:
        backfill_daily(conn, today)
        backfill_monthly(conn, today)
        backfill_usd_krw(conn)
    print("[backfill] done.")


if __name__ == "__main__":
    main()
