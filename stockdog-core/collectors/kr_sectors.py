"""KR 업종(sector) rotation collector — P2/Phase C of the 국장/KR page.

Fetches the full 업종(industry) list with per-sector 등락률 + 상승/하락 종목수
from Naver Finance's mobile industry endpoint:

    GET https://m.stock.naver.com/api/stocks/industry?page=1&pageSize=100

The response carries a `groups` array (one row per 업종, ~79 groups), each:
    {"no":290, "name":"반도체와반도체장비", "totalCount":42,
     "changeRate":"-6.70", "riseCount":3, "fallCount":38, "steadyCount":1}

mapping to:
    name        → 업종명
    changeRate  → 등락률 (%, ALREADY SIGNED string)  ← trust directly
    riseCount   → advancing (상승 종목수)
    fallCount   → declining (하락 종목수)
    steadyCount → steady    (보합 종목수)
    totalCount  → members   (구성 종목수)

⚠️ SIGN LESSON: `changeRate` is returned ALREADY SIGNED (e.g. "-6.70", "0.45").
Trust it directly — do NOT re-apply any separate direction sign (that was the
kr_naver_quote double-negation bug that rendered a crash as a rally). On a DOWN
day the vast majority of 등락률 are NEGATIVE (verified 2026-06-26: 76 of 79 < 0).

We use this mobile JSON source (the SAME host/style kr_breadth.py already
fetches) rather than the finance.naver.com EUC-KR HTML 업종 page — no
encoding/regex fragility, one clean JSON. FREE, no auth, T-0 same-day.

`requests` only (already a dep). Mirrors the DEFENSIVE style of
collectors/kr_breadth.py & kr_investor_flow.py: every failure path logs a
warning and returns None — the KR pipeline is NEVER aborted by this best-effort
key.
"""
import logging

import requests

logger = logging.getLogger(__name__)

_INDUSTRY_URL = (
    "https://m.stock.naver.com/api/stocks/industry?page=1&pageSize=100"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "(+stockdog kr_sectors)"
    ),
    "Referer": "https://m.stock.naver.com/",
}
_TIMEOUT = 10


def _parse_signed_pct(s):
    """'-6.70' / '0.45' / '+1.2' → float %. ALREADY SIGNED — trust as-is.

    Bad/empty input → None. NO separate direction sign applied (sign lesson).
    """
    if s is None:
        return None
    try:
        cleaned = str(s).replace(",", "").replace("+", "").strip()
        if cleaned in ("", "-"):
            return None
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_int(s):
    """'42' / 3 / '1,318' → int. Bad/empty → None."""
    if s is None:
        return None
    try:
        cleaned = str(s).replace(",", "").strip()
        if cleaned in ("", "-"):
            return None
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def fetch_kr_sectors():
    """Full KR 업종(sector) list with signed 등락률 + 상승/하락 종목수.

    Returns a list (sorted by change_pct DESC, leaders first):
        [
          {"name": "반도체와반도체장비", "change_pct": -6.70,
           "advancing": 3, "declining": 38, "steady": 1, "members": 42},
          ...
        ]
    or None on any failure (caller drops the sectors block → emitter hides it).

    Best-effort & tolerant: NEVER raises.
    """
    try:
        resp = requests.get(_INDUSTRY_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning(f"kr_sectors fetch failed, ignoring: {e}")
        return None

    if not isinstance(payload, dict):
        logger.warning("kr_sectors: response not a dict")
        return None
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        logger.warning("kr_sectors: no groups in response")
        return None

    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = g.get("name")
        change_pct = _parse_signed_pct(g.get("changeRate"))
        # A sector with no name or no parseable 등락률 is useless — drop it.
        if not name or change_pct is None:
            continue
        out.append({
            "name": str(name),
            "change_pct": change_pct,           # ALREADY SIGNED (trust)
            "advancing": _parse_int(g.get("riseCount")),
            "declining": _parse_int(g.get("fallCount")),
            "steady": _parse_int(g.get("steadyCount")),
            "members": _parse_int(g.get("totalCount")),
        })

    if not out:
        logger.warning("kr_sectors: no usable sectors parsed, returning None")
        return None

    # Sort leaders→laggards (Naver already returns this order, but make it
    # explicit so downstream top/bottom slicing is deterministic).
    out.sort(key=lambda s: s["change_pct"], reverse=True)
    return out
