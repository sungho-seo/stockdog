"""KR market breadth (등락 종목수) collector — Phase A1 of the 국장/KR page.

Fetches the per-market 상승/보합/하락/상한/하한 종목수 (advance-decline counts)
for KOSPI & KOSDAQ from Naver Finance's mobile index integration endpoint:

    GET https://m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/integration

The response carries an `upDownStockInfo` object:
    {"upperCount":"3","riseCount":"756","lowerCount":"0",
     "fallCount":"144","steadyCount":"18"}   # comma-strings
mapping to:
    riseCount  → up        (상승)
    steadyCount→ flat       (보합)
    fallCount  → down       (하락)
    upperCount → limit_up   (상한)
    lowerCount → limit_down (하한)

We use this mobile JSON source (the SAME host/style kr_naver_quote.py already
fetches for index volume) rather than the finance.naver.com EUC-KR HTML page —
no encoding/regex fragility, one clean JSON. Confirmed live 2026-06-13:
KOSPI 상승756/보합18/하락144/상한3/하한0.

FREE, no auth, T-0 same-day. `requests` only (already a dep). Mirrors the
DEFENSIVE style of collectors/kr_naver_quote.py & kr_investor_flow.py: every
failure path logs a warning and returns None / degrades that single market —
the KR pipeline is NEVER aborted by this best-effort key.
"""
import logging

import requests

logger = logging.getLogger(__name__)

_INTEGRATION_URL = "https://m.stock.naver.com/api/index/{market}/integration"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "(+stockdog kr_breadth)"
    ),
    "Referer": "https://m.stock.naver.com/",
}
_TIMEOUT = 8
_MARKETS = ("KOSPI", "KOSDAQ")

# upDownStockInfo Naver key → our snapshot key.
_FIELD_MAP = {
    "up": "riseCount",
    "flat": "steadyCount",
    "down": "fallCount",
    "limit_up": "upperCount",
    "limit_down": "lowerCount",
}


def _parse_count(s):
    """'756' / '1,318' / '0' → int. Bad/empty → None."""
    if s is None:
        return None
    try:
        cleaned = str(s).replace(",", "").strip()
        if cleaned in ("", "-"):
            return None
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def _get_json(url):
    """GET → parsed JSON or None (never raises)."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"kr_breadth GET {url} failed, ignoring: {e}")
        return None


def _fetch_one_market(market):
    """One market's breadth → {up,flat,down,limit_up,limit_down} or None.

    Any unparseable/absent upDownStockInfo → None (caller drops this market).
    NEVER raises.
    """
    payload = _get_json(_INTEGRATION_URL.format(market=market))
    if not isinstance(payload, dict):
        return None
    info = payload.get("upDownStockInfo")
    if not isinstance(info, dict):
        logger.warning(f"kr_breadth {market}: no upDownStockInfo in response")
        return None
    out = {our: _parse_count(info.get(naver)) for our, naver in _FIELD_MAP.items()}
    # If the two load-bearing counts (up/down) are both missing, this is useless.
    if out.get("up") is None and out.get("down") is None:
        logger.warning(f"kr_breadth {market}: up/down both unparseable")
        return None
    return out


def fetch_market_breadth():
    """Market breadth (등락 종목수) for KOSPI & KOSDAQ.

    Returns:
        {
          "KOSPI":  {"up": 756, "flat": 18, "down": 144,
                     "limit_up": 3, "limit_down": 0},
          "KOSDAQ": {...},
        }
      or None when BOTH markets fail (nothing useful to render).

    Best-effort & tolerant: a single market failing still returns the other.
    NEVER raises.
    """
    out = {}
    for market in _MARKETS:
        data = _fetch_one_market(market)
        if data is not None:
            out[market] = data
    if not out:
        logger.warning("kr_breadth: no market breadth collected, returning None")
        return None
    return out
