"""KR market-level investor flows (투자자별 수급) collector — P2 of the 국장/KR page.

Fetches market-level net-buy 거래대금 (억원) for KOSPI & KOSDAQ split by the
three investor buckets — 개인(individual) / 외국인(foreign) / 기관(institutional)
— from Naver Finance's mobile trend endpoint:

    GET https://m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/trend

The latest row carries `bizdate` ("YYYYMMDD") plus `personalValue` /
`foreignValue` / `institutionalValue` — each a SIGNED, comma-formatted STRING
in 억원 (e.g. "-43,174", "+22,041"). Net-buy sum ≈ 0 (zero-sum sanity).

FREE, no auth, T-0 same-day. NO new pip dependency (`requests` only — already
used across collectors). Mirrors the DEFENSIVE style of collectors/kr_indices.py:
every failure path returns None / logs a warning and NEVER raises, so the KR
pipeline is never aborted by this best-effort 4th key.
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

# Naver mobile finance trend endpoint. Market-level investor net-buy 거래대금.
_TREND_URL = "https://m.stock.naver.com/api/index/{market}/trend"

# Per-STOCK trend endpoint (K7 대형주 트래커). Returns a LIST of daily rows
# (newest first); each row carries individualPureBuyQuant / foreignerPureBuyQuant
# / organPureBuyQuant (signed comma-strings in 주 — SHARES, NOT 억원) plus
# foreignerHoldRatio ("47.63%") and bizdate. Confirmed live against 005930.
_STOCK_TREND_URL = "https://m.stock.naver.com/api/stock/{code}/trend"

# Polite delay between the per-stock calls so we don't hammer Naver.
_STOCK_DELAY_S = 0.4

# Identifying browser-ish UA + Referer (Naver's mobile API expects these).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "(+stockdog kr_investor_flow)"
    ),
    "Referer": "https://m.stock.naver.com/",
}
_TIMEOUT = 8
_MARKETS = ("KOSPI", "KOSDAQ")


def _parse_signed_eok(s):
    """'-43,174' / '+22,041' / '22870' → int 억원. Bad input → None."""
    if s is None:
        return None
    try:
        cleaned = str(s).replace(",", "").replace("+", "").strip()
        if cleaned in ("", "-"):
            return None
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def _yyyymmdd_to_iso(s):
    """'20260612' → '2026-06-12'. 형식 이상 시 None."""
    if not s or len(str(s)) != 8 or not str(s).isdigit():
        return None
    s = str(s)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _latest_trend_row(payload):
    """Extract the latest trend row dict from the Naver response.

    The endpoint returns the latest row as a bare dict
    ({bizdate, personalValue, foreignValue, institutionalValue}); guard for a
    list-wrapped shape too (defensive — take the first element). None on miss.
    """
    if isinstance(payload, dict):
        if "bizdate" in payload:
            return payload
        # Defensive: some Naver endpoints wrap rows under a list key.
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "bizdate" in v[0]:
                return v[0]
        return None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return None


def _fetch_one_market(market):
    """Fetch one market's latest investor flows. Returns
    (bizdate_iso, {individual, foreign, institutional}) or (None, None) on any
    failure — NEVER raises.
    """
    url = _TREND_URL.format(market=market)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        row = _latest_trend_row(resp.json())
        if not row:
            logger.warning(f"kr_investor_flow {market}: no trend row in response")
            return None, None
        flows = {
            "individual": _parse_signed_eok(row.get("personalValue")),
            "foreign": _parse_signed_eok(row.get("foreignValue")),
            "institutional": _parse_signed_eok(row.get("institutionalValue")),
        }
        if all(v is None for v in flows.values()):
            logger.warning(f"kr_investor_flow {market}: all values unparseable")
            return None, None
        bizdate_iso = _yyyymmdd_to_iso(row.get("bizdate"))
        return bizdate_iso, flows
    except Exception as e:
        logger.warning(f"kr_investor_flow {market} fetch failed, ignoring: {e}")
        return None, None


def fetch_market_investor_flows():
    """Market-level investor flows for KOSPI & KOSDAQ.

    Returns:
        {
          "data_date": "2026-06-12",   # bizdate of the data (best-effort)
          "unit": "억원",
          "market": {
            "KOSPI":  {"individual": -43174, "foreign": 22041, "institutional": 22870},
            "KOSDAQ": {"individual":  -9416, "foreign":  2769, "institutional":  6315},
          }
        }
      or None when BOTH markets fail (nothing useful to render).

    Best-effort & tolerant: a single market failing still returns the other;
    when the two bizdates disagree we stamp the first available (the snapshot
    records the actual data_date — no fabrication). NEVER raises.
    """
    market_out = {}
    data_date = None
    for market in _MARKETS:
        bizdate_iso, flows = _fetch_one_market(market)
        if flows is not None:
            market_out[market] = flows
            if data_date is None and bizdate_iso:
                data_date = bizdate_iso

    if not market_out:
        logger.warning("kr_investor_flow: no market data collected, returning None")
        return None

    return {
        "data_date": data_date,
        "unit": "억원",
        "market": market_out,
    }


# ---------------------------------------------------------------------------
# Per-STOCK investor flows (K7 대형주 트래커). Net-buy direction per stock in
# 주(SHARES) — NOT comparable across stocks (different price/float), so the
# emitter renders DIRECTION arrows (sign), not magnitude bars. Separate unit
# from the market-level hero (억원), kept distinct on purpose.
# ---------------------------------------------------------------------------
def _parse_signed_shares(s):
    """'+2,880,306' / '-5,933,301' / '0' → int 주(shares). Bad input → None."""
    if s is None:
        return None
    try:
        cleaned = str(s).replace(",", "").replace("+", "").strip()
        if cleaned in ("", "-"):
            return None
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_ratio_pct(s):
    """'47.63%' → 47.63 (float). Bad/empty input → None."""
    if s is None:
        return None
    try:
        cleaned = str(s).replace("%", "").replace(",", "").strip()
        if cleaned in ("", "-"):
            return None
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _latest_stock_row(payload):
    """Extract the newest per-stock trend row (dict with bizdate) from the
    per-stock endpoint response. The endpoint returns a LIST newest-first; we
    take the first row carrying a bizdate. Defensive for a dict-wrapped shape
    too. None on miss.
    """
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict) and "bizdate" in row:
                return row
        return None
    if isinstance(payload, dict):
        if "bizdate" in payload:
            return payload
        for v in payload.values():
            if isinstance(v, list):
                for row in v:
                    if isinstance(row, dict) and "bizdate" in row:
                        return row
    return None


def _fetch_one_stock_flow(code):
    """Fetch one stock's latest investor flows. Returns a dict
    {individual, foreign, institutional, foreign_ratio, bizdate} or None on any
    failure — NEVER raises. bizdate is ISO ('YYYY-MM-DD') or None.
    """
    url = _STOCK_TREND_URL.format(code=code)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        row = _latest_stock_row(resp.json())
        if not row:
            logger.warning(f"kr_investor_flow stock {code}: no trend row in response")
            return None
        flows = {
            "individual": _parse_signed_shares(row.get("individualPureBuyQuant")),
            "foreign": _parse_signed_shares(row.get("foreignerPureBuyQuant")),
            "institutional": _parse_signed_shares(row.get("organPureBuyQuant")),
            "foreign_ratio": _parse_ratio_pct(row.get("foreignerHoldRatio")),
            "bizdate": _yyyymmdd_to_iso(row.get("bizdate")),
        }
        # If all three net-buy buckets are unparseable, this row is useless.
        if all(
            flows[k] is None for k in ("individual", "foreign", "institutional")
        ):
            logger.warning(f"kr_investor_flow stock {code}: all net-buy values unparseable")
            return None
        return flows
    except Exception as e:
        logger.warning(f"kr_investor_flow stock {code} fetch failed, ignoring: {e}")
        return None


def fetch_stock_investor_flows(codes):
    """Per-stock investor flows for the K7 대형주 basket.

    codes: iterable of 6-digit 종목코드 strings.

    Returns:
        {
          "005930": {"individual": -5933301, "foreign": 2880306,
                     "institutional": 3295009, "foreign_ratio": 47.63,
                     "bizdate": "2026-06-12"},
          ...
        }
      — values in 주(SHARES). A code that fails is OMITTED (partial dict);
      returns {} when every code fails (nothing useful). NEVER raises.

    A small polite delay separates the per-stock calls.
    """
    out = {}
    codes = list(codes or [])
    for i, code in enumerate(codes):
        flow = _fetch_one_stock_flow(code)
        if flow is not None:
            out[str(code)] = flow
        if i < len(codes) - 1:
            time.sleep(_STOCK_DELAY_S)
    if not out:
        logger.warning("kr_investor_flow: no per-stock flows collected, returning {}")
    return out


# ---------------------------------------------------------------------------
# 종목단위 외국인 연속 (Phase A2). "외국인 N일 연속 순매수/순매도" — the run of
# consecutive trading days, counted from the MOST RECENT day backward, over
# which a single stock's daily foreign net-buy kept the SAME sign. Uses the
# per-stock /trend 10-day history (foreignerPureBuyQuant signed). A flat (0)
# or unparseable day breaks the run. Direction "buy"(+) / "sell"(−).
# ---------------------------------------------------------------------------
def _stock_trend_rows(payload):
    """All per-stock /trend rows NEWEST-FIRST (list of dicts w/ bizdate). []."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict) and "bizdate" in r]
    if isinstance(payload, dict):
        if "bizdate" in payload:
            return [payload]
        for v in payload.values():
            if isinstance(v, list):
                rows = [r for r in v if isinstance(r, dict) and "bizdate" in r]
                if rows:
                    return rows
    return []


def _streak_from_quants(quants):
    """quants: foreign net-buy ints NEWEST-FIRST (None allowed).

    → {"streak_days": int, "direction": "buy"|"sell"|None}. The run is the
    number of leading entries sharing the sign of the newest non-None entry;
    a sign flip, a zero, or a None terminates it. All-None/empty → days 0,
    direction None.
    """
    direction = None
    days = 0
    for q in quants:
        if q is None or q == 0:
            break
        sign = "buy" if q > 0 else "sell"
        if direction is None:
            direction = sign
            days = 1
        elif sign == direction:
            days += 1
        else:
            break
    return {"streak_days": days, "direction": direction}


def compute_foreign_streak(code):
    """외국인 연속 순매수/순매도 streak for one stock (Phase A2).

    Fetches the per-stock /trend 10-day history and computes the consecutive-
    same-direction foreign net-buy run from the most recent day backward.

    Returns {"streak_days": int, "direction": "buy"|"sell"|None} — days 0 /
    direction None when no usable data (fetch fail / all flat). NEVER raises.
    """
    url = _STOCK_TREND_URL.format(code=code)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        rows = _stock_trend_rows(resp.json())
        if not rows:
            logger.warning(f"kr_investor_flow streak {code}: no trend rows")
            return {"streak_days": 0, "direction": None}
        quants = [_parse_signed_shares(r.get("foreignerPureBuyQuant")) for r in rows]
        return _streak_from_quants(quants)
    except Exception as e:
        logger.warning(f"kr_investor_flow streak {code} failed, ignoring: {e}")
        return {"streak_days": 0, "direction": None}


def fetch_foreign_streaks(codes):
    """Per-stock foreign 연속 streak for the K7 basket.

    codes: iterable of 6-digit 종목코드 strings.

    Returns {code: {"streak_days": int, "direction": "buy"|"sell"|None}, ...}.
    A code that yields no usable history is OMITTED (partial dict); {} when all
    fail. A small polite delay separates the per-stock calls. NEVER raises.
    """
    out = {}
    codes = list(codes or [])
    for i, code in enumerate(codes):
        st = compute_foreign_streak(code)
        # Keep only meaningful streaks (≥1 day w/ a direction); drop empties.
        if st and st.get("streak_days") and st.get("direction"):
            out[str(code)] = st
        if i < len(codes) - 1:
            time.sleep(_STOCK_DELAY_S)
    return out
