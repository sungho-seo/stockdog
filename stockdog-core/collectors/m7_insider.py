"""
SEC EDGAR Form 4 (insider transactions) collector for M7 tickers.

Flow:
  1. submissions/CIK<10-digit>.json — recent.form == "4" 최근 N일
  2. accession_number별로 primary XML 다운로드 → <nonDerivativeTransaction> 파싱
  3. shares × price = USD value
  4. dedupe by accession_number

URLs (SEC EDGAR):
  - https://data.sec.gov/submissions/CIK<10-digit>.json
  - https://www.sec.gov/Archives/edgar/data/<cik-int>/<accession-nodash>/<accession>.txt
    또는 같은 디렉토리의 *.xml (primary doc)

Headers (SEC 정책):
  - User-Agent 필수 (회사명 + 이메일 — config에서 주입)
  - Accept-Encoding gzip 권장

Retry (cfg.fetch.edgar_*):
  - 5xx/429/timeout만 retry (3회), exponential backoff 2→4→8s
  - 4xx (404/403)은 즉시 fail (Form 4 없거나 잘못된 URL)

Output (per ticker):
  [{accession, date, insider_name, role, action, shares, price_usd, value_usd, breach}]

Per-ticker try/except — 한 종목 실패해도 나머지 6개 계속.
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}"

# Form 4 action 매핑 (transactionCode):
#  P (Purchase), S (Sale), A (Grant), M (Exercise), G (Gift), F (TaxWithholding), etc.
ACTION_LABEL = {
    "P": "Buy",
    "S": "Sell",
    "A": "Grant",
    "M": "Exercise",
    "G": "Gift",
    "F": "TaxWithholding",
    "D": "Disposition",
    "I": "Discretionary",
    "V": "Voluntary",
}

# Market transaction codes (P=Purchase, S=Sale). All others are non-market (exercises, grants, etc.)
MARKET_CODES = {"P", "S"}


def _retry_get(
    url: str,
    headers: Dict[str, str],
    max_retries: int,
    backoff_sec: float,
    pause_sec: float,
    label: str = "",
) -> Optional[requests.Response]:
    """Retry GET with exponential backoff on 5xx/429/timeout.

    4xx (except 429) → 즉시 fail (None 반환).
    매 요청 후 pause_sec sleep (SEC rate limit 보호 — 10 req/s 미만 유지).
    """
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            # SEC rate limit politeness
            time.sleep(pause_sec)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429,) or 500 <= resp.status_code < 600:
                last_err = f"HTTP {resp.status_code}"
                if attempt < max_retries:
                    sleep_s = backoff_sec * (2 ** attempt)
                    logger.warning(f"[{label}] {url} → {resp.status_code}, retry in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                logger.error(f"[{label}] {url} → {resp.status_code} after {max_retries} retries")
                return None
            # 4xx (404/403 등) → 즉시 fail
            logger.warning(f"[{label}] {url} → {resp.status_code} (no retry, 4xx)")
            return None
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = str(e)
            if attempt < max_retries:
                sleep_s = backoff_sec * (2 ** attempt)
                logger.warning(f"[{label}] {url} timeout/conn-err: {e}, retry in {sleep_s:.1f}s")
                time.sleep(sleep_s)
                continue
            logger.error(f"[{label}] {url} failed after {max_retries} retries: {e}")
            return None
        except Exception as e:
            logger.error(f"[{label}] {url} unexpected error: {e}")
            return None
    logger.error(f"[{label}] {url} all attempts failed: {last_err}")
    return None


def _normalize_cik10(cik: str) -> str:
    """CIK를 10-digit zero-pad. config에서 이미 zero-pad 됐으면 그대로."""
    digits = re.sub(r"\D", "", str(cik))
    return digits.zfill(10)


def _verify_cik_symbol(submissions_json: Dict[str, Any], expected_symbol: str) -> bool:
    """submissions.json의 tickers 배열에 expected_symbol이 있는지 확인.

    GOOG/GOOGL은 같은 CIK이고 tickers는 ["GOOG","GOOGL"] 양쪽 포함. case-insensitive 비교.
    """
    tickers = submissions_json.get("tickers") or []
    norm = {str(t).upper() for t in tickers if t}
    return expected_symbol.upper() in norm


def _fetch_submissions(
    cik10: str,
    headers: Dict[str, str],
    max_retries: int,
    backoff_sec: float,
    pause_sec: float,
) -> Optional[Dict[str, Any]]:
    url = SEC_SUBMISSIONS_URL.format(cik10=cik10)
    resp = _retry_get(url, headers, max_retries, backoff_sec, pause_sec, label=f"submissions:{cik10}")
    if not resp:
        return None
    try:
        return resp.json()
    except ValueError as e:
        logger.error(f"submissions JSON parse failed for {cik10}: {e}")
        return None


def _list_recent_form4(
    submissions: Dict[str, Any],
    lookback_days: int,
    today: date,
) -> List[Tuple[str, str, str]]:
    """submissions.filings.recent에서 form=='4'인 항목 중 lookback_days 안의 것 추출.

    Returns: [(accession_number, filing_date, primary_document)]
    filing_date 형식: "YYYY-MM-DD" (SEC API 표준).
    primary_document: ".xml" 또는 "_doc4.xml" 같은 파일명.
    """
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []

    cutoff = today - timedelta(days=lookback_days)
    out: List[Tuple[str, str, str]] = []
    for form, acc, fdate, doc in zip(forms, accs, dates, primary_docs):
        if form != "4":
            continue
        try:
            d = datetime.strptime(fdate, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        out.append((acc, fdate, doc))
    return out


def _fetch_form4_xml(
    cik_int: str,
    accession: str,
    primary_doc: str,
    headers: Dict[str, str],
    max_retries: int,
    backoff_sec: float,
    pause_sec: float,
) -> Optional[str]:
    """Form 4 primary XML 본문 다운로드.

    URL: archives/edgar/data/<cik-int>/<accession-nodash>/<primary_doc>

    중요: SEC submissions API의 primary_doc은 보통 "xslF345X06/form4.xml" 같은
    XSL-rendered HTML 경로. raw XML은 같은 디렉토리에 "form4.xml" (xsl 폴더 prefix 없이)로
    존재. 따라서 xslF345X06/ 같은 prefix를 제거한 raw 파일을 우선 시도하고, 실패하면 원
    경로로 fallback.
    """
    acc_nodash = accession.replace("-", "")
    base = SEC_ARCHIVES_BASE.format(cik_int=cik_int, accession_nodash=acc_nodash)

    # primary_doc이 "xslF345X??/form?.xml" 패턴이면 raw XML은 prefix 제거판
    raw_doc = primary_doc
    if "/" in primary_doc:
        # 예: "xslF345X06/form4.xml" → "form4.xml"
        raw_doc = primary_doc.split("/")[-1]

    candidates = []
    if raw_doc != primary_doc:
        candidates.append(raw_doc)
    candidates.append(primary_doc)

    for doc in candidates:
        url = f"{base}/{doc}"
        resp = _retry_get(url, headers, max_retries, backoff_sec, pause_sec, label=f"form4:{accession}:{doc}")
        if not resp:
            continue
        text = resp.text
        # raw XML 검증: <ownershipDocument> 루트 또는 <?xml prelude
        head = text.lstrip()[:200].lower()
        if "ownershipdocument" in head or head.startswith("<?xml"):
            return text
        logger.debug(f"[form4:{accession}] {doc} not raw XML (HTML wrapper), trying next candidate")
    return None


def _safe_text(el: Optional[ET.Element], path: str) -> str:
    if el is None:
        return ""
    found = el.find(path)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def _parse_form4_xml(xml_text: str, accession: str) -> Dict[str, Any]:
    """Form 4 XML → 단일 dict (집계, market transactions only).

    동일 Form 4 안에 여러 nonDerivativeTransaction 가능 → 본 함수는 각 transaction을
    list로 분해하지 않고 합산한 단일 레코드를 반환 (M7 트래커 임계값은 USD 금액 기준이라
    sub-transaction 단위 노이즈보다 accession 단위 합산이 유의미).

    IMPORTANT: Market transactions (P=Purchase, S=Sale) are tracked separately from non-market
    (M=Exercise, A=Grant, G=Gift, F=TaxWithholding, etc.). The headline (action/shares/price/value)
    is computed from market transactions only. Non-market transactions are tracked in separate fields.

    Output:
      {
        accession, date (transaction date), insider_name, role,
        action (대표 액션 — market only, or "Exercise"/"Other" for non-market-only),
        shares (market shares, headline), price_usd (market weighted-avg, headline),
        value_usd (market USD, headline),
        market_shares, market_value_usd, nonmarket_shares, nonmarket_value_usd,
        tx_codes (sorted list of all transactionCodes seen),
        price_footnoted (bool, if any market row had price<=0)
      }
    """
    out: Dict[str, Any] = {
        "accession": accession,
        "date": None,
        "insider_name": "",
        "role": "",
        "action": "",
        "shares": 0.0,
        "price_usd": 0.0,
        "value_usd": 0.0,
        "security_titles": [],
        "market_shares": 0.0,
        "market_value_usd": 0.0,
        "nonmarket_shares": 0.0,
        "nonmarket_value_usd": 0.0,
        "tx_codes": [],
        "price_footnoted": False,
    }
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning(f"Form 4 XML parse failed [{accession}]: {e}")
        return out

    # ownership document — element 이름은 namespace 없는 경우가 일반적
    # reportingOwner/reportingOwnerId/rptOwnerName
    owner_el = root.find(".//reportingOwner")
    if owner_el is not None:
        out["insider_name"] = _safe_text(owner_el, "reportingOwnerId/rptOwnerName")
        rel = owner_el.find("reportingOwnerRelationship")
        roles = []
        if rel is not None:
            for tag, label in (
                ("isDirector", "Director"),
                ("isOfficer", "Officer"),
                ("isTenPercentOwner", "10%Owner"),
                ("isOther", "Other"),
            ):
                txt = _safe_text(rel, tag)
                if txt in ("1", "true", "True"):
                    roles.append(label)
            title = _safe_text(rel, "officerTitle")
            if title:
                roles.append(title)
        out["role"] = ", ".join(roles)

    # Track market and non-market separately
    mkt_buy_shares = 0.0
    mkt_buy_value = 0.0
    mkt_buy_weighted_num = 0.0
    mkt_buy_weighted_den = 0.0

    mkt_sell_shares = 0.0
    mkt_sell_value = 0.0
    mkt_sell_weighted_num = 0.0
    mkt_sell_weighted_den = 0.0

    nonmkt_shares = 0.0
    nonmkt_value = 0.0
    nonmkt_codes: set = set()

    latest_tx_date = None
    sec_titles: List[str] = []
    all_codes: set = set()

    for tx in root.findall(".//nonDerivativeTransaction"):
        title = _safe_text(tx, "securityTitle/value")
        if title and title not in sec_titles:
            sec_titles.append(title)

        tx_date = _safe_text(tx, "transactionDate/value")
        if tx_date:
            try:
                d = datetime.strptime(tx_date, "%Y-%m-%d").date()
                if latest_tx_date is None or d > latest_tx_date:
                    latest_tx_date = d
            except ValueError:
                pass

        code = _safe_text(tx, "transactionCoding/transactionCode")
        ad = _safe_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value")
        # A = Acquired (buy direction), D = Disposed (sell direction)
        shares_txt = _safe_text(tx, "transactionAmounts/transactionShares/value")
        price_txt = _safe_text(tx, "transactionAmounts/transactionPricePerShare/value")
        try:
            shares = float(shares_txt) if shares_txt else 0.0
        except ValueError:
            shares = 0.0
        try:
            price = float(price_txt) if price_txt else 0.0
        except ValueError:
            price = 0.0

        if code:
            all_codes.add(code)

        # Classify by transactionCode
        if code in MARKET_CODES:
            # Market transaction
            tx_value = abs(shares * price)
            if ad == "A":  # Acquired (buy)
                mkt_buy_shares += abs(shares)
                mkt_buy_value += tx_value
                if price > 0:
                    mkt_buy_weighted_num += abs(shares) * price
                    mkt_buy_weighted_den += abs(shares)
                else:
                    out["price_footnoted"] = True
            elif ad == "D":  # Disposed (sell)
                mkt_sell_shares += abs(shares)
                mkt_sell_value += tx_value
                if price > 0:
                    mkt_sell_weighted_num += abs(shares) * price
                    mkt_sell_weighted_den += abs(shares)
                else:
                    out["price_footnoted"] = True
        else:
            # Non-market transaction (exercise, grant, gift, etc.)
            nonmkt_shares += abs(shares)
            nonmkt_value += abs(shares * price)
            if code:
                nonmkt_codes.add(code)

    # Headline derivation: prioritize market transactions
    if mkt_buy_value > 0 or mkt_sell_value > 0:
        # Market transactions present — pick dominant direction
        if mkt_buy_value > mkt_sell_value:
            out["action"] = "Buy"
            out["shares"] = mkt_buy_shares
            out["value_usd"] = mkt_buy_value
            if mkt_buy_weighted_den > 0:
                out["price_usd"] = round(mkt_buy_weighted_num / mkt_buy_weighted_den, 4)
        else:
            out["action"] = "Sell"
            out["shares"] = mkt_sell_shares
            out["value_usd"] = mkt_sell_value
            if mkt_sell_weighted_den > 0:
                out["price_usd"] = round(mkt_sell_weighted_num / mkt_sell_weighted_den, 4)
    elif nonmkt_shares > 0:
        # Only non-market transactions
        out["shares"] = 0.0  # Headline shares = 0 for non-market
        out["value_usd"] = 0.0  # Headline value = 0 for non-market
        out["price_usd"] = 0.0
        if len(nonmkt_codes) == 1:
            code = next(iter(nonmkt_codes))
            out["action"] = ACTION_LABEL.get(code, code)
        else:
            out["action"] = "Other" if nonmkt_codes else ""
    else:
        # No transactions
        out["action"] = ""

    # Market and non-market fields (for detailed tracking)
    out["market_shares"] = mkt_buy_shares + mkt_sell_shares
    out["market_value_usd"] = mkt_buy_value + mkt_sell_value
    out["nonmarket_shares"] = nonmkt_shares
    out["nonmarket_value_usd"] = nonmkt_value

    # All transaction codes seen (for debugging and filtering)
    out["tx_codes"] = sorted(list(all_codes))

    if latest_tx_date:
        out["date"] = latest_tx_date.isoformat()
    out["security_titles"] = sec_titles
    return out


def collect_insider_for_ticker(
    ticker: str,
    cik: str,
    cfg: Dict[str, Any],
    min_value_usd: float,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """단일 ticker insider 수집.

    Returns:
      {
        "ticker": ticker,
        "transactions": [tx_dict, ...],   # dedupe by accession
        "verified_cik": bool,             # tickers 배열에 symbol 매칭됐는지
        "filings_scanned": int,
        "error": str | None,
      }
    """
    fetch = cfg.get("fetch", {}) or {}
    headers = {
        "User-Agent": fetch.get("sec_user_agent", "Skyler M7 Tracker nosy.seo@gmail.com"),
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }
    max_retries = int(fetch.get("edgar_max_retries", 3))
    backoff_sec = float(fetch.get("edgar_retry_backoff_sec", 2.0))
    pause_sec = float(fetch.get("edgar_request_pause_sec", 0.15))
    lookback_days = int(fetch.get("form4_lookback_days", 4))
    if today is None:
        # KST today 기준 — 시장 close 기준 영업일 contextually 더 안전하지만
        # Form 4는 filing date 기준이라 KST/UTC 차이 무시 가능.
        today = datetime.utcnow().date()

    result: Dict[str, Any] = {
        "ticker": ticker,
        "transactions": [],
        "verified_cik": False,
        "filings_scanned": 0,
        "error": None,
    }

    cik10 = _normalize_cik10(cik)
    cik_int = str(int(cik10))  # leading zero 제거 → archives URL용

    submissions = _fetch_submissions(cik10, headers, max_retries, backoff_sec, pause_sec)
    if submissions is None:
        result["error"] = f"submissions fetch failed (CIK {cik10})"
        return result

    result["verified_cik"] = _verify_cik_symbol(submissions, ticker)
    if not result["verified_cik"]:
        # 정합성 위반 — 즉시 fail, 다른 종목 보존
        result["error"] = (
            f"CIK {cik10} submissions.tickers does not include {ticker!r} "
            f"(got: {submissions.get('tickers')})"
        )
        logger.error(result["error"])
        return result

    form4_entries = _list_recent_form4(submissions, lookback_days, today)
    result["filings_scanned"] = len(form4_entries)

    # archives.sec.gov는 다른 Host 헤더 — 새 headers
    archive_headers = dict(headers)
    archive_headers["Host"] = "www.sec.gov"
    archive_headers["Accept"] = "application/xml, text/xml, */*"

    seen_accessions = set()
    for accession, filing_date, primary_doc in form4_entries:
        if accession in seen_accessions:
            continue
        seen_accessions.add(accession)
        if not primary_doc or not primary_doc.lower().endswith(".xml"):
            # Form 4 primary doc은 보통 .xml. 아니면 skip.
            logger.debug(f"[{ticker}] {accession} primary_doc={primary_doc!r} not .xml, skipping")
            continue
        xml_text = _fetch_form4_xml(cik_int, accession, primary_doc, archive_headers,
                                    max_retries, backoff_sec, pause_sec)
        if not xml_text:
            continue
        tx = _parse_form4_xml(xml_text, accession)
        # transaction date 없으면 filing_date로 폴백
        if not tx.get("date"):
            tx["date"] = filing_date
        tx["breach"] = tx.get("value_usd", 0.0) >= float(min_value_usd)
        tx["filing_date"] = filing_date
        result["transactions"].append(tx)

    return result


def collect_all(cfg: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """모든 M7 ticker insider 수집.

    Returns:
      {
        "by_ticker": {ticker: result_dict},
        "errors": {ticker: error_str},
        "filings_total": int,
        "transactions_total": int,
      }
    """
    from utils.m7_config import get_tickers, merge_thresholds

    by_ticker: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    filings_total = 0
    transactions_total = 0

    for t in get_tickers(cfg):
        symbol = t["symbol"]
        cik = t["cik"]
        eff = merge_thresholds(symbol, cfg)
        min_value_usd = float(eff.get("insider", {}).get("min_value_usd", 500000))
        try:
            res = collect_insider_for_ticker(symbol, cik, cfg, min_value_usd, today=today)
            by_ticker[symbol] = res
            filings_total += res.get("filings_scanned", 0)
            transactions_total += len(res.get("transactions", []))
            if res.get("error"):
                errors[symbol] = res["error"]
        except Exception as e:
            logger.exception(f"[{symbol}] insider collect crashed: {e}")
            errors[symbol] = f"crash: {e}"
            by_ticker[symbol] = {
                "ticker": symbol,
                "transactions": [],
                "verified_cik": False,
                "filings_scanned": 0,
                "error": f"crash: {e}",
            }

    return {
        "by_ticker": by_ticker,
        "errors": errors,
        "filings_total": filings_total,
        "transactions_total": transactions_total,
    }
