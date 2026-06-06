"""IMPR-061 / IMPR-068 / IMPR-070: backfill macro history into metrics_history.db (container-side).

Idempotent / re-runnable: every write is a column-scoped UPSERT keyed by date
(daily) or (series, obs_date) (monthly), so re-running converges to the same
state — it never duplicates and never clobbers other columns (M1-safe).

Sources:
  - FRED daily series (yields / spread / fed funds / broad dollar / HY spread / VIX):
      IMPR-070: last ~550 days (~1.5yr trading days) — was 120 days
  - FRED weekly series (ICSA jobless claims): last 2 years (~104 weekly points)
  - FRED monthly series (CPI / Core CPI / PPI / Core PCE / UNRATE):
      IMPR-070: last ~36 months (~1100 days) — was 24 months (~740 days)
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

from collectors.fred_macro import DAILY_SERIES, WEEKLY_SERIES, MONTHLY_SERIES, fetch_history  # noqa: E402
from utils.metrics_history import _conn, DB_PATH  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _span(pairs):
    """Return 'first..last' date span for a list of (date, value), or '—'."""
    if not pairs:
        return "—"
    return f"{pairs[0][0]}..{pairs[-1][0]}"


def backfill_daily(conn, today):
    # IMPR-070: extended from 120 days to 550 days (~1.5 trading years) for period-toggle.
    # This also backfills vix (VIXCLS) added to DAILY_SERIES in IMPR-070.
    start = (today - timedelta(days=550)).isoformat()
    print(f"[backfill] daily series since {start}")
    for col, series_id in DAILY_SERIES.items():
        pairs = fetch_history(series_id, start)
        for d, v in pairs:
            conn.execute("INSERT OR IGNORE INTO market_metrics (date) VALUES (?)", (d,))
            conn.execute(f"UPDATE market_metrics SET {col}=? WHERE date=?", (v, d))
        print(f"  {col:<10} ({series_id:<14}) {len(pairs):>4} obs  [{_span(pairs)}]")


def backfill_weekly(conn, today):
    """IMPR-068: ICSA jobless claims — weekly, stored sparse in market_metrics.

    2 years back (~104 weekly observations; ICSA releases every Thursday).
    Stored on the FRED observation date (the Thursday of each week) so the chart
    x-axis aligns to the actual release week, not the pipeline run date.
    """
    start = (today - timedelta(days=730)).isoformat()
    print(f"[backfill] weekly series since {start}")
    for col, series_id in WEEKLY_SERIES.items():
        pairs = fetch_history(series_id, start)
        for d, v in pairs:
            conn.execute("INSERT OR IGNORE INTO market_metrics (date) VALUES (?)", (d,))
            conn.execute(f"UPDATE market_metrics SET {col}=? WHERE date=?", (v, d))
        print(f"  {col:<10} ({series_id:<14}) {len(pairs):>4} obs  [{_span(pairs)}]")


def backfill_monthly(conn, today):
    # IMPR-070: extended from ~24 months (740 days) to ~36 months (1100 days).
    # Deeper monthly history ensures YoY computation covers the full range
    # and the dashboard can show 3-year inflation/unemployment charts.
    start = (today - timedelta(days=1100)).isoformat()
    print(f"[backfill] monthly series since {start}")
    for series, series_id in MONTHLY_SERIES.items():
        pairs = fetch_history(series_id, start)
        for d, v in pairs:
            conn.execute(
                "INSERT OR REPLACE INTO macro_monthly (series, obs_date, level) VALUES (?,?,?)",
                (series, d, v)
            )
        print(f"  {series:<10} ({series_id:<14}) {len(pairs):>4} obs  [{_span(pairs)}]")


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
        backfill_weekly(conn, today)
        backfill_monthly(conn, today)
        backfill_usd_krw(conn)
    print("[backfill] done.")


if __name__ == "__main__":
    main()
