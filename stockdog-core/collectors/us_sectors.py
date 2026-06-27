"""
US GICS Sector Rotation Tracker (11 sector ETFs).

Fetches 11 sector ETFs via yfinance:
  XLK (Technology), XLC (Communication), XLY (Consumer Discretionary),
  XLP (Consumer Staples), XLE (Energy), XLF (Financials),
  XLV (Healthcare), XLI (Industrials), XLB (Materials),
  XLU (Utilities), XLRE (Real Estate)

Per-ETF metrics:
  - close: latest closing price
  - d1: 1-day change % (last vs prior session)
  - d5: 5-session change % (last vs 5 sessions ago)
  - d1mo: ~1-month change % (last vs first in 3-month window, ~21 sessions)
  - momentum: d5 + d1mo (score)
  - rank: 1-11 by momentum descending

Output (call collect_us_sectors()):
  {
    "schema_version": 1,
    "asof": "YYYY-MM-DD" (last index date, US session date),
    "generated": "<UTC iso>",
    "source": "yfinance",
    "sectors": [
      {
        "etf": "XLK",
        "name": "Technology",
        "close": 154.32,
        "d1": 0.45,
        "d5": 2.13,
        "d1mo": -1.23,
        "momentum": 0.90,
        "rank": 1
      },
      ...
    ]
  }

Per-ETF try/except — one bad ticker doesn't kill the batch. Nulls its fields, keeps going.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import yfinance as yf

logger = logging.getLogger(__name__)

SECTOR_ETFS = [
    ("XLK", "Technology"),
    ("XLC", "Communication"),
    ("XLY", "Consumer Discretionary"),
    ("XLP", "Consumer Staples"),
    ("XLE", "Energy"),
    ("XLF", "Financials"),
    ("XLV", "Healthcare"),
    ("XLI", "Industrials"),
    ("XLB", "Materials"),
    ("XLU", "Utilities"),
    ("XLRE", "Real Estate"),
]


def _fetch_one(etf: str, name: str) -> Optional[Dict[str, Any]]:
    """Fetch single ETF 3-month history. Returns dict or None on failure."""
    try:
        # Use Ticker.history() pattern (consistent with asia_overnight.py)
        hist = yf.Ticker(etf).history(period="3mo")
        if hist.empty or len(hist) < 2:
            logger.warning(f"[us_sectors] insufficient data for {etf}: {len(hist)} rows")
            return None

        # Extract close values - iloc returns scalar for Series
        close = round(float(hist["Close"].iloc[-1]), 2)
        prev_close = round(float(hist["Close"].iloc[-2]), 2)

        # d1: last vs prior session
        d1 = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

        # d5: last vs 5 sessions ago (if available)
        if len(hist) >= 6:
            close_5ago = round(float(hist["Close"].iloc[-6]), 2)
            d5 = round((close - close_5ago) / close_5ago * 100, 2) if close_5ago else 0.0
        else:
            # Less than 6 rows; use what we have
            close_5ago = round(float(hist["Close"].iloc[0]), 2)
            d5 = round((close - close_5ago) / close_5ago * 100, 2) if close_5ago else 0.0

        # d1mo: last vs first in window (~21 sessions ≈ 1 month)
        # If less than 22 rows, use earliest available
        if len(hist) >= 22:
            close_1mo_ago = round(float(hist["Close"].iloc[-22]), 2)
        else:
            close_1mo_ago = round(float(hist["Close"].iloc[0]), 2)
        d1mo = round((close - close_1mo_ago) / close_1mo_ago * 100, 2) if close_1mo_ago else 0.0

        # momentum = d5 + d1mo
        momentum = round(d5 + d1mo, 2)

        # asof date: last index date (trading day)
        asof = hist.index[-1].strftime("%Y-%m-%d")

        return {
            "etf": etf,
            "name": name,
            "close": close,
            "d1": d1,
            "d5": d5,
            "d1mo": d1mo,
            "momentum": momentum,
            "asof": asof,
        }
    except Exception as e:
        logger.error(f"[us_sectors] failed to fetch {etf}: {e}")
        return None


def collect_us_sectors() -> Dict[str, Any]:
    """
    Fetch all 11 sector ETFs and compute metrics.

    Returns:
      {
        "schema_version": 1,
        "asof": "YYYY-MM-DD",
        "generated": "<UTC iso>",
        "source": "yfinance",
        "sectors": [
          {etf, name, close, d1, d5, d1mo, momentum, rank},
          ...
        ]
      }

    Per-ETF failures are nulled but don't kill the batch.
    """
    sectors: List[Dict[str, Any]] = []
    asof = None

    print("[us_sectors] fetching 11 GICS sector ETFs...")
    for etf, name in SECTOR_ETFS:
        print(f"  {etf} ({name})...", end=" ", flush=True)
        data = _fetch_one(etf, name)
        if data:
            sectors.append(data)
            if asof is None:
                asof = data["asof"]
            print("✓")
        else:
            # null fields for failed ticker
            sectors.append({
                "etf": etf,
                "name": name,
                "close": None,
                "d1": None,
                "d5": None,
                "d1mo": None,
                "momentum": None,
                "asof": None,
            })
            print("✗")

    # Rank by momentum descending (skip nulls)
    valid = [s for s in sectors if s.get("momentum") is not None]
    valid.sort(key=lambda s: s["momentum"], reverse=True)

    # Assign ranks
    rank_map = {s["etf"]: i + 1 for i, s in enumerate(valid)}
    for s in sectors:
        s["rank"] = rank_map.get(s["etf"])

    if asof is None:
        asof = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "schema_version": 1,
        "asof": asof,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "yfinance",
        "sectors": sectors,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    import json
    out = collect_us_sectors()
    print("\n=== US Sectors Collector Result ===")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nTotal ETFs: {len(out['sectors'])} / {len(SECTOR_ETFS)}")
    print(f"As-of: {out['asof']}")
