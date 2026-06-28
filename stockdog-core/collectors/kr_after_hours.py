"""KR 시간외(NXT) 급변동 collector — P2/Phase D of the 국장/KR page.

Computes the AFTER-HOURS move for a small KR equity universe: the change of the
NXT 시간외 단일가 (20:00 KST) vs the SAME ticker's regular KRX 15:30 close. This
is the "다음날 갭" signal 국장 watchers read in the evening — the differentiated
after-hours layer the rest of the /kr page does not surface.

    after-hours move (%) = (NXT 20:00 단일가 − KRX 15:30 종가) / KRX 15:30 종가 × 100

Source = the SAME free Naver mobile endpoint the K7 collector already uses:

    GET https://m.stock.naver.com/api/stock/{code}/basic

which carries BOTH prices in one response:
  * closePrice                       = KRX 15:30 regular-session close.
  * overMarketPriceInfo.overPrice    = NXT 20:00 after-hours single-price close,
    present ONLY when an after-hours session ran for that ticker and exposed once
    `overMarketStatus == "CLOSE"` (settled).

SIGN — IMPORTANT (project memory: the kr_naver_quote double-negation bug). We do
NOT read Naver's `fluctuationsRatio` for this card: that field is signed vs the
PREVIOUS-DAY close, not vs today's regular session. The after-hours SIGNAL we want
is the move FROM today's regular close, so we compute it directly from the two
prices — (over − close)/close. The sign falls out of the arithmetic; there is NO
separate direction code applied, so there is no double-negation risk. Both prices
are read verbatim from Naver (already-correct values).

DEFENSIVE / never-raise (mirrors kr_sectors.py / kr_naver_quote.py): every failure
path logs a warning and degrades that single item or returns None — the KR
pipeline is NEVER aborted by this best-effort key. On a weekend / holiday the
endpoint returns the LAST settled session (same staleness as the rest of the page,
keyed by `data_date`); when no ticker has a settled NXT price the block degrades to
empty items (emitter shows a graceful "데이터 없음" empty-state) or None.
"""
import logging
import time
from collections import Counter

import requests

logger = logging.getLogger(__name__)

_STOCK_BASIC_URL = "https://m.stock.naver.com/api/stock/{code}/basic"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "(+stockdog kr_after_hours)"
    ),
    "Referer": "https://m.stock.naver.com/",
}
_TIMEOUT = 8
_STOCK_DELAY_S = 0.4
# Only surface a meaningful after-hours move on the card. Sub-threshold (and
# exactly-flat) names are dropped — a ±0.01% print is noise, not 급변동. Kept
# small so a quiet large-cap session still shows its real movers.
_MIN_ABS_PCT = 0.10


def _parse_num(s):
    """'8,123.62' / '322,500' / '15040' → float. Bad/empty → None."""
    if s is None:
        return None
    try:
        cleaned = str(s).replace(",", "").replace("+", "").strip()
        if cleaned in ("", "-"):
            return None
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _trade_date_from_iso(local_traded_at):
    """'2026-06-26T20:00:00+09:00' → '2026-06-26'. Bad → None."""
    if not local_traded_at:
        return None
    try:
        date_part = str(local_traded_at).split("T", 1)[0]  # '2026-06-26'
        ymd = date_part.replace("-", "")
        if len(ymd) == 8 and ymd.isdigit():
            return date_part
    except Exception:
        pass
    return None


def _get_json(url):
    """GET → parsed JSON or None (never raises)."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"kr_after_hours GET {url} failed, ignoring: {e}")
        return None


def _one_after_hours(code):
    """One code → after-hours item dict or None. Never raises.

    Returns {name, code, reg_close, nxt_price, change_pct, date, settled} where
    change_pct = (nxt − reg_close)/reg_close × 100 (SIGNED by arithmetic). None
    when the ticker has no settled NXT after-hours price for the session.
    """
    basic = _get_json(_STOCK_BASIC_URL.format(code=code))
    if not isinstance(basic, dict):
        return None
    reg_close = _parse_num(basic.get("closePrice"))
    if reg_close is None or reg_close == 0:
        return None
    nxt = basic.get("overMarketPriceInfo")
    if not isinstance(nxt, dict):
        return None  # no after-hours session for this ticker
    over = _parse_num(nxt.get("overPrice"))
    if over is None:
        return None
    settled = str(nxt.get("overMarketStatus", "")).strip().upper() == "CLOSE"
    # after-hours move vs the REGULAR close (NOT vs prev-day) — computed directly
    # from the two prices, no direction code (no double-negation).
    change_pct = round((over - reg_close) / reg_close * 100.0, 2)
    date = _trade_date_from_iso(nxt.get("localTradedAt"))
    return {
        "name": basic.get("stockName"),
        "code": str(code),
        "reg_close": int(round(reg_close)),
        "nxt_price": int(round(over)),
        "change_pct": change_pct,
        "date": date,
        "settled": settled,
    }


def fetch_after_hours(codes):
    """KR 시간외(NXT) 급변동 block for a list of 6-digit codes.

    Returns:
        {
          "data_date": "YYYY-MM-DD" | None,   # NXT after-hours session date
          "session": "closed" | "open",        # all settled → closed; any live → open
          "items": [
            {"name","code","reg_close","nxt_price","change_pct","volume":None}, ...
          ]                                     # sorted by change_pct DESC (gainers first)
        }
    or None on total failure / no usable input. `items` may be EMPTY (the session
    ran but no ticker had a ≥_MIN_ABS_PCT move) — the emitter then shows a graceful
    empty-state. Best-effort & tolerant: NEVER raises.
    """
    codes = [str(c) for c in (codes or []) if c]
    # de-dup, preserve order
    seen = set()
    uniq = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    if not uniq:
        return None

    items = []
    dates = []
    any_live = False
    any_session = False
    for i, code in enumerate(uniq):
        rec = _one_after_hours(code)
        if rec is not None:
            any_session = True
            if not rec["settled"]:
                any_live = True
            if rec["date"]:
                dates.append(rec["date"])
            # threshold filter — keep only meaningful 급변동.
            if abs(rec["change_pct"]) >= _MIN_ABS_PCT:
                items.append({
                    "name": rec["name"],
                    "code": rec["code"],
                    "reg_close": rec["reg_close"],
                    "nxt_price": rec["nxt_price"],
                    "change_pct": rec["change_pct"],
                    "volume": None,  # NXT-only volume not exposed on /basic
                })
        if i < len(uniq) - 1:
            time.sleep(_STOCK_DELAY_S)

    if not any_session:
        # No ticker exposed any after-hours session at all → no block.
        logger.warning("kr_after_hours: no after-hours session for any code")
        return None

    items.sort(key=lambda it: it["change_pct"], reverse=True)
    data_date = None
    if dates:
        data_date = Counter(dates).most_common(1)[0][0]
    return {
        "data_date": data_date,
        "session": "open" if any_live else "closed",
        "items": items,
    }
