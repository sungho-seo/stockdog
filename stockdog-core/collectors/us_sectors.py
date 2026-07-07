"""
US GICS Sector Rotation Tracker (11 sector ETFs) + Theme ETFs (5 themes).

Fetches 11 sector ETFs via yfinance:
  XLK (Technology), XLC (Communication), XLY (Consumer Discretionary),
  XLP (Consumer Staples), XLE (Energy), XLF (Financials),
  XLV (Healthcare), XLI (Industrials), XLB (Materials),
  XLU (Utilities), XLRE (Real Estate)

Plus 5 theme/industry ETFs:
  SMH (반도체), IGV (소프트웨어), MAGS (M7 메가캡),
  CIBR (사이버보안), QTUM (양자컴퓨팅)

Per-ETF metrics:
  - close: latest closing price
  - d1: 1-day change % (last vs prior session)
  - d5: 5-session change % (last vs 5 sessions ago)
  - d1mo: ~1-month change % (last vs first in 3-month window, ~21 sessions)
  - momentum: d5 + d1mo (score)
  - rank: 1-11 (sectors) or 1-5 (themes) by momentum descending

Output (call collect_us_sectors()):
  {
    "schema_version": 2,
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
    ],
    "themes": [
      {
        "etf": "SMH",
        "name": "반도체",
        "close": 234.56,
        "d1": 1.23,
        "d5": 3.45,
        "d1mo": 2.10,
        "momentum": 5.55,
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

from utils.prior_close import prior_from_history

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

THEME_ETFS = [
    ("SMH", "반도체"),
    ("IGV", "소프트웨어"),
    ("MAGS", "M7 메가캡"),
    ("CIBR", "사이버보안"),
    ("QTUM", "양자컴퓨팅"),
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
        prior = prior_from_history(hist)
        if prior.value is not None and prior.within_window:
            prev_close = round(prior.value, 2)
            d1 = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0
        else:
            # No valid prior within window → never compute against a stale baseline.
            # d1=None (consumer narrative_common.py filters `d1 is not None`); prev=current.
            prev_close = close
            d1 = None

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
    Fetch all 11 sector ETFs and 5 theme ETFs, compute metrics.

    Returns:
      {
        "schema_version": 2,
        "asof": "YYYY-MM-DD",
        "generated": "<UTC iso>",
        "source": "yfinance",
        "sectors": [
          {etf, name, close, d1, d5, d1mo, momentum, rank},
          ...
        ],
        "themes": [
          {etf, name, close, d1, d5, d1mo, momentum, rank},
          ...
        ]
      }

    Per-ETF failures are nulled but don't kill the batch.
    """
    sectors: List[Dict[str, Any]] = []
    themes: List[Dict[str, Any]] = []
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

    # Rank sectors by momentum descending (skip nulls)
    valid_sectors = [s for s in sectors if s.get("momentum") is not None]
    valid_sectors.sort(key=lambda s: s["momentum"], reverse=True)

    # Assign sector ranks
    sector_rank_map = {s["etf"]: i + 1 for i, s in enumerate(valid_sectors)}
    for s in sectors:
        s["rank"] = sector_rank_map.get(s["etf"])

    print("[us_sectors] fetching 5 theme ETFs...")
    for etf, name in THEME_ETFS:
        print(f"  {etf} ({name})...", end=" ", flush=True)
        data = _fetch_one(etf, name)
        if data:
            themes.append(data)
            if asof is None:
                asof = data["asof"]
            print("✓")
        else:
            # null fields for failed ticker
            themes.append({
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

    # Rank themes by momentum descending (skip nulls)
    valid_themes = [t for t in themes if t.get("momentum") is not None]
    valid_themes.sort(key=lambda t: t["momentum"], reverse=True)

    # Assign theme ranks (separate from sectors)
    theme_rank_map = {t["etf"]: i + 1 for i, t in enumerate(valid_themes)}
    for t in themes:
        t["rank"] = theme_rank_map.get(t["etf"])

    if asof is None:
        asof = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "schema_version": 2,
        "asof": asof,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "yfinance",
        "sectors": sectors,
        "themes": themes,
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
    print(f"\nTotal Sector ETFs: {len(out['sectors'])} / {len(SECTOR_ETFS)}")
    print(f"Total Theme ETFs: {len(out['themes'])} / {len(THEME_ETFS)}")
    print(f"Schema Version: {out['schema_version']}")
    print(f"As-of: {out['asof']}")
