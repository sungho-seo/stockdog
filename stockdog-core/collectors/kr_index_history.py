"""KR index 30-day price history (지수 추세 스파크라인) — Phase A3 of /kr.

Fetches the trailing ~30 daily closes for KOSPI & KOSDAQ from Naver's mobile
index price endpoint:

    GET https://m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/price?pageSize=30

The response is a LIST of daily rows NEWEST-FIRST; each carries `closePrice`
(comma-string, e.g. "8,123.62") and `localTradedAt` ("2026-06-12"). We reverse
to OLDEST→NEWEST so the emitter can draw a left→right polyline directly.

FREE, no auth, T-0. `requests` only. DEFENSIVE: any failure → None for that
market (caller drops it); the sparkline simply isn't rendered. NEVER raises.
Confirmed live 2026-06-13: 30 closes returned for both markets.
"""
import logging

import requests

logger = logging.getLogger(__name__)

_PRICE_URL = "https://m.stock.naver.com/api/index/{market}/price?pageSize={n}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "(+stockdog kr_index_history)"
    ),
    "Referer": "https://m.stock.naver.com/",
}
_TIMEOUT = 8
_MARKETS = ("KOSPI", "KOSDAQ")
_PAGE_SIZE = 30


def _parse_close(s):
    """'8,123.62' → 8123.62 (float). Bad/empty → None."""
    if s is None:
        return None
    try:
        cleaned = str(s).replace(",", "").strip()
        if cleaned in ("", "-"):
            return None
        return round(float(cleaned), 2)
    except (ValueError, TypeError):
        return None


def _get_json(url):
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"kr_index_history GET {url} failed, ignoring: {e}")
        return None


def _fetch_one_market(market, n=_PAGE_SIZE):
    """One market's closes OLDEST→NEWEST or None. NEVER raises.

    Returns {"closes": [..floats.., newest last], "count": int} or None.
    """
    payload = _get_json(_PRICE_URL.format(market=market, n=n))
    rows = None
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        # Defensive: some Naver endpoints wrap the list under a key.
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                rows = v
                break
    if not rows:
        logger.warning(f"kr_index_history {market}: no rows in response")
        return None
    # Response is NEWEST-FIRST → reverse to OLDEST→NEWEST for the polyline.
    closes = []
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        c = _parse_close(row.get("closePrice"))
        if c is not None:
            closes.append(c)
    if not closes:
        logger.warning(f"kr_index_history {market}: no parseable closes")
        return None
    return {"closes": closes, "count": len(closes)}


def fetch_index_history():
    """30-day index close history for KOSPI & KOSDAQ (sparkline source).

    Returns:
        {
          "KOSPI":  {"closes": [oldest..newest], "count": 30},
          "KOSDAQ": {...},
        }
      or None when BOTH markets fail. Best-effort: a single market failing
      still returns the other. NEVER raises.
    """
    out = {}
    for market in _MARKETS:
        data = _fetch_one_market(market)
        if data is not None:
            out[market] = data
    if not out:
        logger.warning("kr_index_history: no history collected, returning None")
        return None
    return out
