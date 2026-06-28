"""KR price/index AUTHORITATIVE source — Naver Finance (same-day, T-0, NXT 20:00 close).

Replaces data.go.kr (`kr_indices.py` / `kr_stocks.py`) as the authoritative KR
price/index source. data.go.kr 주식시세/지수 is **T+1 published** — it never
returns same-day data, so the old pipeline silently fell back to T-1 and labeled
it "fresh" (the /kr page showed YESTERDAY's prices). Naver's mobile finance API
gives the same-day close (T-0), the SAME source K7 수급/외인% already come from
(collectors/kr_investor_flow.py).

NXT vs KRX close — IMPORTANT. The 국장 now has an after-hours NXT session that
settles at **20:00 KST**, and that 20:00 NXT print is the price 국장 watchers
treat as "today's close". The `/api/stock/{code}/basic` response carries BOTH:
  * `closePrice`                       = the **KRX 15:30 regular-session close**.
  * `overMarketPriceInfo.overPrice`    = the **NXT 20:00 after-hours close**.
This collector uses `overMarketPriceInfo.overPrice` (NXT 20:00) as the
authoritative stock `close` once that after-hours session has SETTLED
(`overMarketPriceInfo.overMarketStatus == "CLOSE"`), and falls back to
`closePrice` (15:30 KRX) when there is no after-hours session for that ticker
(`overMarketPriceInfo` absent — e.g. names with no NXT trade) or it has not yet
closed. Indices have NO NXT session, so the index collector always uses
`closePrice` (15:30) unchanged.

This collector returns the SAME dict shape as the data.go.kr collectors
(get_kr_index_data / get_kr_stock_data) so the snapshot/pipeline consume it
unchanged:

    {ticker: {name, type, close, prev_close, change_pct, volume, market,
              base_date: 'YYYYMMDD' | None}}

Endpoints (FREE, no auth, confirmed live 2026-06-12):
  * Index:  GET https://m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/basic
              → closePrice, compareToPreviousClosePrice (ALREADY-SIGNED, e.g.
                "-519.09"), compareToPreviousPrice.code (direction, informational
                only), fluctuationsRatio (ALREADY-SIGNED %, e.g. "-5.81"),
                localTradedAt (ISO → trading date). No volume here.
            GET .../{KOSPI|KOSDAQ}/integration → totalInfos 거래량 ("493,406천주").
  * Stock:  GET https://m.stock.naver.com/api/stock/{code}/basic
              → closePrice (15:30 KRX close), compareToPreviousClosePrice,
                compareToPreviousPrice, fluctuationsRatio, marketStatus,
                localTradedAt, stockExchangeType, AND (when an after-hours NXT
                session ran) overMarketPriceInfo = {overPrice (20:00 NXT close),
                compareToPreviousPrice.code (direction, informational only),
                fluctuationsRatio (ALREADY-SIGNED % vs PREV-DAY close),
                compareToPreviousClosePrice (ALREADY-SIGNED vs prev-day close),
                localTradedAt
                ("...T20:00:00+09:00"), overMarketStatus ("CLOSE" after 20:00)}.
            GET .../{code}/integration → totalInfos 거래량 + 전일 (prev_close) +
                market. NOTE: /integration 거래량 is the **NXT-inclusive (KRX+NXT
                consolidated)** day total — verified empirically (it equals the
                KRX-only /trend volume on non-NXT days but ~2x on NXT-active days,
                e.g. 삼성전자 06-12 = 60.07M consolidated vs 30.72M KRX-only).
                Do NOT "correct" this to /trend — that would DROP NXT volume.

Direction code (Naver convention) — INFORMATIONAL ONLY. The numeric fields
(fluctuationsRatio / compareToPreviousClosePrice) are ALREADY SIGNED, so we trust
them directly and do NOT multiply by a derived sign (doing so double-negated DOWN
days — fixed 2026-06-28). The code mapping, for reference:
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
    # Naver returns ALREADY-SIGNED values (verified live: fluctuationsRatio
    # "-5.81", compareToPreviousClosePrice "-519.09" on a DOWN day). Trust them
    # directly. The previous code multiplied by a separately-derived direction
    # sign → DOUBLE-NEGATED down days (rendered UP) and broke prev_close.
    change_pct = _parse_num(basic.get("fluctuationsRatio"))
    if change_pct is not None:
        change_pct = round(change_pct, 2)
    signed_compare = _parse_num(basic.get("compareToPreviousClosePrice"))
    prev_close = None
    if signed_compare is not None:
        prev_close = round(close - signed_compare, 2)
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


def _nxt_close_fields(basic):
    """If a settled NXT 20:00 after-hours close exists, return its
    (close, change_pct, prev_close, base_date); else None.

    Source = basic['overMarketPriceInfo'] (present only when an after-hours NXT
    session ran for this ticker). We use it ONLY when that session has SETTLED
    (overMarketStatus == 'CLOSE'); a still-open / pre-close session falls back to
    the 15:30 KRX closePrice. Fully defensive — any malformed field returns None
    so the caller degrades to the regular-session logic. Never raises.
    """
    if not isinstance(basic, dict):
        return None
    nxt = basic.get("overMarketPriceInfo")
    if not isinstance(nxt, dict):
        return None
    if str(nxt.get("overMarketStatus", "")).strip().upper() != "CLOSE":
        return None
    close = _parse_num(nxt.get("overPrice"))
    if close is None:
        return None
    # Naver NXT fields are ALREADY-SIGNED (same as the regular session) — trust
    # them directly; do NOT re-apply a direction sign (would double-negate).
    change_pct = _parse_num(nxt.get("fluctuationsRatio"))
    if change_pct is not None:
        change_pct = round(change_pct, 2)
    signed_compare = _parse_num(nxt.get("compareToPreviousClosePrice"))
    prev_close = None
    if signed_compare is not None:
        prev_close = int(round(close - signed_compare))
    # base_date: NXT localTradedAt is the same trading day as the KRX session;
    # prefer it but fall back to the basic-level date below if absent.
    base_date = _trade_date_from_iso(nxt.get("localTradedAt"))
    return {
        "close": int(round(close)),
        "change_pct": change_pct,
        "prev_close": prev_close,
        "base_date": base_date,
    }


def _fetch_one_stock(code):
    """One stock → standard price dict or None. Never raises.

    Stock `close` is the **NXT 20:00 after-hours close** when that session has
    settled (basic.overMarketPriceInfo.overMarketStatus == 'CLOSE'); otherwise
    the **15:30 KRX regular close** (closePrice). See module docstring.
    """
    basic = _get_json(_STOCK_BASIC_URL.format(code=code))
    if not isinstance(basic, dict):
        return None
    # Regular-session (15:30 KRX) baseline — also the fallback when no NXT close.
    close = _parse_num(basic.get("closePrice"))
    if close is None:
        logger.warning(f"kr_naver_quote stock {code}: no closePrice")
        return None
    # Naver returns ALREADY-SIGNED values — trust them directly (see
    # _fetch_one_index). Do NOT re-apply a direction sign (double-negation bug).
    change_pct = _parse_num(basic.get("fluctuationsRatio"))
    if change_pct is not None:
        change_pct = round(change_pct, 2)
    signed_compare = _parse_num(basic.get("compareToPreviousClosePrice"))
    prev_close = None
    if signed_compare is not None:
        prev_close = int(round(close - signed_compare))
    base_date = _trade_date_from_iso(basic.get("localTradedAt"))
    market = _market_from_exchange(basic.get("stockExchangeType"))

    # Prefer the settled NXT 20:00 after-hours close when available.
    nxt = _nxt_close_fields(basic)
    if nxt is not None:
        close = float(nxt["close"])
        change_pct = nxt["change_pct"]
        prev_close = nxt["prev_close"]
        if nxt["base_date"]:
            base_date = nxt["base_date"]

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
