"""KR price/index AUTHORITATIVE source — Naver Finance (same-day, T-0, NXT-inclusive).

Replaces data.go.kr (`kr_indices.py` / `kr_stocks.py`) as the authoritative KR
price/index source. data.go.kr 주식시세/지수 is **T+1 published** — it never
returns same-day data, so the old pipeline silently fell back to T-1 and labeled
it "fresh" (the /kr page showed YESTERDAY's prices). Naver's mobile finance API
gives the **same-day close (T-0, NXT-inclusive, timestamp ~18:59)**, the SAME
source K7 수급/외인% already come from (collectors/kr_investor_flow.py).

This collector returns the SAME dict shape as the data.go.kr collectors
(get_kr_index_data / get_kr_stock_data) so the snapshot/pipeline consume it
unchanged:

    {ticker: {name, type, close, prev_close, change_pct, volume, market,
              base_date: 'YYYYMMDD' | None}}

Endpoints (FREE, no auth, confirmed live 2026-06-12):
  * Index:  GET https://m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/basic
              → closePrice, compareToPreviousClosePrice (UNSIGNED magnitude),
                compareToPreviousPrice.code (direction), fluctuationsRatio
                (UNSIGNED %), localTradedAt (ISO → trading date). No volume here.
            GET .../{KOSPI|KOSDAQ}/integration → totalInfos 거래량 ("493,406천주").
  * Stock:  GET https://m.stock.naver.com/api/stock/{code}/basic
              → closePrice, compareToPreviousClosePrice, compareToPreviousPrice,
                fluctuationsRatio, marketStatus, localTradedAt, stockExchangeType.
            GET .../{code}/integration → totalInfos 거래량 (plain shares) + 전일
                (prev_close) + market.

Direction code (Naver convention) decides the SIGN of the unsigned
fluctuationsRatio / compareToPreviousClosePrice:
    1=상한, 2=상승  → UP   (+)
    3=보합          → FLAT (0)
    4=하한, 5=하락  → DOWN (−)

DEFENSIVE / never-raise (mirrors kr_investor_flow.py): every failure path logs a
warning and returns None / degrades that single item — the KR pipeline is NEVER
aborted. Freshness is surfaced HONESTLY via base_date (the trading date the data
is actually as-of) so the pipeline's stale-callout logic works correctly; we do
NOT pretend stale data is fresh.
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

_INDEX_BASIC_URL = "https://m.stock.naver.com/api/index/{market}/basic"
_INDEX_INTEGRATION_URL = "https://m.stock.naver.com/api/index/{market}/integration"
_STOCK_BASIC_URL = "https://m.stock.naver.com/api/stock/{code}/basic"
_STOCK_INTEGRATION_URL = "https://m.stock.naver.com/api/stock/{code}/integration"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "(+stockdog kr_naver_quote)"
    ),
    "Referer": "https://m.stock.naver.com/",
}
_TIMEOUT = 8
_STOCK_DELAY_S = 0.4

# Naver ticker → index-name (matches data.go.kr collector's index_name_map keys).
_INDEX_MARKETS = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ"}

# Direction codes that imply a NEGATIVE move (하한 / 하락).
_DOWN_CODES = {"4", "5"}
_FLAT_CODES = {"3"}


# ---------------------------------------------------------------------------
# Parsers (reuse the comma-string convention of kr_investor_flow._parse_*).
# ---------------------------------------------------------------------------
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


def _parse_vol_cheonju(s):
    """Index 거래량 '493,406천주' → 493406000 (int 주). Bad → None.

    천주 = thousand shares (×1000). Stock 거래량 is plain shares (no 천주 suffix);
    this helper multiplies by 1000 ONLY when the '천주' marker is present.
    """
    if s is None:
        return None
    raw = str(s)
    mult = 1000 if "천주" in raw else 1
    cleaned = raw.replace("천주", "").replace("주", "").replace(",", "").strip()
    if cleaned in ("", "-"):
        return None
    try:
        return int(float(cleaned) * mult)
    except (ValueError, TypeError):
        return None


def _direction_sign(compare_to_prev):
    """compareToPreviousPrice dict → +1 / 0 / -1 (sign multiplier).

    Falls back to the Korean text / English name when the code is unexpected.
    Default +1 (up) when nothing is parseable — but callers cross-check against
    the unsigned magnitude so a wrong default is self-evident in QA.
    """
    if not isinstance(compare_to_prev, dict):
        return 1
    code = str(compare_to_prev.get("code", "")).strip()
    if code in _DOWN_CODES:
        return -1
    if code in _FLAT_CODES:
        return 0
    if code in ("1", "2"):
        return 1
    # Fallback: text / name.
    text = str(compare_to_prev.get("text", "")) + str(compare_to_prev.get("name", ""))
    if "하락" in text or "하한" in text or "FALLING" in text.upper() or "LOWER" in text.upper():
        return -1
    if "보합" in text or "STEADY" in text.upper() or "UNCHANGED" in text.upper():
        return 0
    return 1


def _trade_date_from_iso(local_traded_at):
    """'2026-06-12T18:59:00+09:00' → '20260612' (YYYYMMDD). Bad → None."""
    if not local_traded_at:
        return None
    try:
        date_part = str(local_traded_at).split("T", 1)[0]  # '2026-06-12'
        ymd = date_part.replace("-", "")
        if len(ymd) == 8 and ymd.isdigit():
            return ymd
    except Exception:
        pass
    return None


def _total_infos_map(payload):
    """integration payload → {key: value} from totalInfos list. {} on miss."""
    if not isinstance(payload, dict):
        return {}
    ti = payload.get("totalInfos")
    if not isinstance(ti, list):
        return {}
    out = {}
    for row in ti:
        if isinstance(row, dict) and "key" in row:
            out[row["key"]] = row.get("value")
    return out


def _get_json(url):
    """GET → parsed JSON or None (never raises)."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"kr_naver_quote GET {url} failed, ignoring: {e}")
        return None


# ---------------------------------------------------------------------------
# Index (KOSPI / KOSDAQ).
# ---------------------------------------------------------------------------
def _fetch_one_index(market):
    """One index → standard dict or None. Never raises."""
    basic = _get_json(_INDEX_BASIC_URL.format(market=market))
    if not isinstance(basic, dict):
        return None
    close = _parse_num(basic.get("closePrice"))
    if close is None:
        logger.warning(f"kr_naver_quote index {market}: no closePrice")
        return None
    sign = _direction_sign(basic.get("compareToPreviousPrice"))
    change_pct_mag = _parse_num(basic.get("fluctuationsRatio"))
    change_pct = None if change_pct_mag is None else round(sign * change_pct_mag, 2)
    compare_mag = _parse_num(basic.get("compareToPreviousClosePrice"))
    prev_close = None
    if compare_mag is not None:
        prev_close = round(close - sign * compare_mag, 2)
    base_date = _trade_date_from_iso(basic.get("localTradedAt"))

    # Volume lives in /integration totalInfos (거래량, 천주).
    volume = None
    integ = _get_json(_INDEX_INTEGRATION_URL.format(market=market))
    if isinstance(integ, dict):
        volume = _parse_vol_cheonju(_total_infos_map(integ).get("거래량"))

    return {
        "close": round(close, 2),
        "prev_close": prev_close,
        "change_pct": change_pct,
        "volume": volume,
        "base_date": base_date,
    }


def get_kr_index_data_naver(items):
    """Naver T-0 replacement for collectors.kr_indices.get_kr_index_data.

    items: list of {ticker ('KOSPI'/'KOSDAQ'), name, type:'INDEX_KR'}.
    Returns {ticker: {name, type, base_date, close, prev_close, change_pct,
                      volume}} — same shape as the data.go.kr collector.
    A ticker that fails is OMITTED (best-effort). NEVER raises.
    """
    results = {}
    for item in items or []:
        ticker = item.get("ticker")
        market = _INDEX_MARKETS.get(ticker)
        if not market:
            logger.warning(f"kr_naver_quote: unknown index ticker {ticker!r}, skipping")
            continue
        print(f"Fetching KR index (Naver T-0) for {ticker}...")
        data = _fetch_one_index(market)
        if data:
            results[ticker] = {
                "name": item.get("name"),
                "type": item.get("type"),
                **data,
            }
        else:
            logger.warning(f"kr_naver_quote: no index data for {ticker}")
    return results


# ---------------------------------------------------------------------------
# Stock (per-code).
# ---------------------------------------------------------------------------
def _market_from_exchange(stock_exchange_type):
    """stockExchangeType dict → 'KOSPI'/'KOSDAQ' (nameEng) or ''."""
    if isinstance(stock_exchange_type, dict):
        return stock_exchange_type.get("nameEng") or stock_exchange_type.get("name") or ""
    return ""


def _fetch_one_stock(code):
    """One stock → standard price dict or None. Never raises."""
    basic = _get_json(_STOCK_BASIC_URL.format(code=code))
    if not isinstance(basic, dict):
        return None
    close = _parse_num(basic.get("closePrice"))
    if close is None:
        logger.warning(f"kr_naver_quote stock {code}: no closePrice")
        return None
    sign = _direction_sign(basic.get("compareToPreviousPrice"))
    change_pct_mag = _parse_num(basic.get("fluctuationsRatio"))
    change_pct = None if change_pct_mag is None else round(sign * change_pct_mag, 2)
    compare_mag = _parse_num(basic.get("compareToPreviousClosePrice"))
    prev_close = None
    if compare_mag is not None:
        prev_close = int(round(close - sign * compare_mag))
    base_date = _trade_date_from_iso(basic.get("localTradedAt"))
    market = _market_from_exchange(basic.get("stockExchangeType"))

    # Volume from /integration totalInfos (거래량, plain shares).
    volume = None
    integ = _get_json(_STOCK_INTEGRATION_URL.format(code=code))
    if isinstance(integ, dict):
        tmap = _total_infos_map(integ)
        volume = _parse_vol_cheonju(tmap.get("거래량"))

    return {
        "close": int(round(close)),
        "prev_close": prev_close,
        "change_pct": change_pct,
        "volume": volume,
        "market": market,
        "base_date": base_date,
    }


def get_kr_stock_data_naver(items):
    """Naver T-0 replacement for collectors.kr_stocks.get_kr_stock_data.

    items: list of {ticker (6-digit code), name, type:'STOCK_KR'/'ETF_KR'}.
    Returns {ticker: {name, type, base_date, close, prev_close, change_pct,
                      volume, market}} — same shape as the data.go.kr collector.
    A code that fails is OMITTED (best-effort, partial dict). NEVER raises.
    """
    results = {}
    items = list(items or [])
    for i, item in enumerate(items):
        ticker = str(item.get("ticker"))
        print(f"Fetching KR stock (Naver T-0) for {ticker} ({item.get('name')})...")
        data = _fetch_one_stock(ticker)
        if data:
            results[ticker] = {
                "name": item.get("name"),
                "type": item.get("type"),
                **data,
            }
        else:
            logger.warning(f"kr_naver_quote: no data for {ticker}")
        if i < len(items) - 1:
            time.sleep(_STOCK_DELAY_S)
    return results
