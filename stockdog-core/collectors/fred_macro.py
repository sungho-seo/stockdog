"""FRED macro series collector (IMPR-061 / IMPR-068).

Stdlib + requests only. Reuses economic_calendar's `_fred()` shape
(os.getenv("FRED_API_KEY"), file_type=json, raise_for_status, timeout=10).

Every fetch is per-series try/except — this module NEVER raises. A failed
series is simply absent from the returned dict (mirrors economic_calendar's
per-series `continue` at :94-96). The US pipeline wraps the whole macro step
in its own try/except as a second layer of defense.

Series groups:
  DAILY   — yields / spread / policy rate / broad dollar / HY spread (FRED daily cadence)
  WEEKLY  — jobless claims (ICSA, weekly Thursday; stored sparse in market_metrics daily table)
  MONTHLY — inflation indices (CPI / Core CPI / PPI / Core PCE), unemployment rate
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"

# key -> FRED series_id. Keys are the column / snapshot names used downstream.
DAILY_SERIES = {
    "us_2y":     "DGS2",          # 2-Year Treasury constant maturity
    "macro_10y": "DGS10",         # 10-Year Treasury constant maturity
    "us_30y":    "DGS30",         # 30-Year Treasury constant maturity
    "t10y2y":    "T10Y2Y",        # 10Y minus 2Y spread (FRED computes it)
    "fed_funds": "DFF",           # Effective federal funds rate (daily)
    "dxy_broad": "DTWEXBGS",      # Nominal Broad U.S. Dollar Index (NOT ICE DXY)
    "hy_spread": "BAMLH0A0HYM2",  # ICE BofA US High Yield OAS (%, daily)
    "vix":       "VIXCLS",        # IMPR-070: CBOE Volatility Index (daily, FRED)
}

# IMPR-068: weekly high-frequency series stored sparse in market_metrics daily table.
# ICSA is released every Thursday; most daily rows will be NULL for this column.
WEEKLY_SERIES = {
    "jobless": "ICSA",  # Initial Jobless Claims (SA, weekly)
}

MONTHLY_SERIES = {
    "cpi":      "CPIAUCSL",   # CPI, all items
    "core_cpi": "CPILFESL",   # CPI less food & energy
    "ppi":      "PPIACO",     # PPI, all commodities
    "pce":      "PCEPILFE",   # Core PCE Price Index — YoY computed same as CPI/PPI
    "unrate":   "UNRATE",     # Civilian Unemployment Rate (%)
}

# Combined map for callers that want everything by key.
ALL_SERIES = {**DAILY_SERIES, **WEEKLY_SERIES, **MONTHLY_SERIES}


def _fred(endpoint, params):
    """Thin FRED GET wrapper. Raises on HTTP error (caller catches)."""
    params = {**params, "api_key": os.getenv("FRED_API_KEY"), "file_type": "json"}
    resp = requests.get(f"{FRED_BASE}/{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_latest(series_id):
    """Return (date, value) of the most recent valid observation, or (None, None).

    Skips FRED's missing-value sentinel ("."). Value rounded to 3 decimals.
    """
    try:
        data = _fred("series/observations", {
            "series_id": series_id,
            "observation_start": "2024-01-01",
            "sort_order": "desc",
            "limit": 1,
        })
    except Exception as e:
        logger.warning(f"FRED fetch_latest failed for {series_id}: {e}")
        return None, None
    obs = [o for o in data.get("observations", []) if o.get("value") not in (".", None, "")]
    if not obs:
        return None, None
    try:
        return obs[0]["date"], round(float(obs[0]["value"]), 3)
    except (TypeError, ValueError):
        return None, None


def fetch_history(series_id, start):
    """Return [(date, value), ...] ascending for a series since `start` (YYYY-MM-DD).

    Drops the "." sentinel; values rounded to 3 decimals. [] on any failure.
    """
    try:
        data = _fred("series/observations", {
            "series_id": series_id,
            "observation_start": start,
            "sort_order": "asc",
        })
    except Exception as e:
        logger.warning(f"FRED fetch_history failed for {series_id}: {e}")
        return []
    out = []
    for o in data.get("observations", []):
        v = o.get("value")
        if v in (".", None, ""):
            continue
        try:
            out.append((o["date"], round(float(v), 3)))
        except (TypeError, ValueError):
            continue
    return out


def get_macro_latest():
    """Fetch the latest observation for every macro series.

    Returns {key: {"date": <YYYY-MM-DD>, "value": <float>}} for every series
    that returned a usable observation. Missing/failed series are simply absent.
    Never raises.
    """
    if not os.getenv("FRED_API_KEY"):
        logger.warning("get_macro_latest: FRED_API_KEY not set, returning {}")
        return {}

    out = {}
    for key, series_id in ALL_SERIES.items():
        try:
            d, v = fetch_latest(series_id)
        except Exception as e:  # belt-and-suspenders; fetch_latest already guards
            logger.warning(f"get_macro_latest: {key} ({series_id}) failed: {e}")
            continue
        if d is not None and v is not None:
            out[key] = {"date": d, "value": v}
    return out
