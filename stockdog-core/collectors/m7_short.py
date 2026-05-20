"""
FINRA RegSHO daily consolidated NMS short volume collector for M7 tickers.

URL: https://cdn.finra.org/equity/regsho/daily/CNMSshvol<YYYYMMDD>.txt
  - Pipe-delimited TXT, ASCII
  - Header line: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
  - 모든 NMS 종목 포함 → M7 7종 필터링

Walkback:
  - 오늘 KST 기준 → today, today-1, today-2 ... 최대 finra_walkback_days (3)
  - 휴장일(NYSE_HOLIDAYS_2026)/주말은 walkback에 카운트 안 함, skip
  - 첫 200 OK 파일 사용

Output (per ticker):
  {
    "date": "YYYY-MM-DD",          # 파일 기준 거래일
    "short_volume": int,
    "total_volume": int,
    "short_ratio": float,           # short_volume / total_volume * 100 (퍼센트)
    "data_as_of": "YYYY-MM-DD",
    "freshness": "fresh" | "stale",
    "walkback_days": int            # 오늘 대비 며칠 거슬렀는지 (영업일 단위)
  }

3-day walkback 모두 실패 시 per-ticker error 필드 + freshness=stale, short_ratio=None.
"""
import csv
import io
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from utils.us_date import candidate_business_dates, kst_today_date

logger = logging.getLogger(__name__)


FINRA_CNMS_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{yyyymmdd}.txt"


def _fetch_finra_file(yyyymmdd: str, ua: str, timeout: int = 20) -> Optional[str]:
    """FINRA CNMS file 다운로드. 200이면 본문, 404 등은 None.

    FINRA CDN은 UA 검증 까다롭지 않으나, SEC와 같은 정책으로 식별 가능 UA 사용.
    """
    url = FINRA_CNMS_URL.format(yyyymmdd=yyyymmdd)
    headers = {
        "User-Agent": ua,
        "Accept": "text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.warning(f"FINRA fetch {yyyymmdd} network err: {e}")
        return None
    except Exception as e:
        logger.error(f"FINRA fetch {yyyymmdd} unexpected: {e}")
        return None

    if resp.status_code == 200:
        return resp.text
    if resp.status_code == 404:
        logger.info(f"FINRA file 404 for {yyyymmdd} (likely not published yet)")
        return None
    logger.warning(f"FINRA fetch {yyyymmdd} HTTP {resp.status_code}")
    return None


def _parse_finra_text(text: str, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Pipe-delimited 파일에서 symbols 행만 추출.

    Returns: {symbol: {date, short_volume, total_volume, short_ratio}}
    """
    sym_set = {s.upper() for s in symbols}
    out: Dict[str, Dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in reader:
        sym = (row.get("Symbol") or "").strip().upper()
        if sym not in sym_set:
            continue
        # FINRA file: ShortVolume/TotalVolume이 정수가 아니라 float string인 케이스 존재
        # (예: "451782.848624"). float→round 후 int 처리.
        try:
            short_vol_raw = row.get("ShortVolume") or "0"
            total_vol_raw = row.get("TotalVolume") or "0"
            short_vol = int(round(float(short_vol_raw)))
            total_vol = int(round(float(total_vol_raw)))
        except (ValueError, TypeError):
            continue
        if total_vol <= 0:
            continue
        ratio = short_vol / total_vol * 100.0
        raw_date = (row.get("Date") or "").strip()
        # FINRA file Date 컬럼은 YYYYMMDD
        iso_date = ""
        if len(raw_date) == 8 and raw_date.isdigit():
            iso_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        out[sym] = {
            "date": iso_date or raw_date,
            "short_volume": short_vol,
            "total_volume": total_vol,
            "short_ratio": round(ratio, 4),
        }
    return out


def collect_all(cfg: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """모든 M7 ticker 공매도 데이터 수집.

    Walkback: today → today-1 → ... NYSE 영업일만 (휴장일/주말 skip),
              최대 finra_walkback_days. 첫 성공 파일 사용.

    Returns:
      {
        "by_ticker": {symbol: record_or_error_dict},
        "file_used": "YYYY-MM-DD" or None,
        "walkback_days": int (0=오늘 사용, 1=어제, ...),  # 영업일 단위
        "errors": {symbol: error_str},  # symbol 별 missing
        "freshness": "fresh" | "stale",  # walkback_days==0이면 fresh, 그 외 stale
      }
    """
    from utils.m7_config import get_ticker_symbols

    fetch = cfg.get("fetch", {}) or {}
    walkback_max = int(fetch.get("finra_walkback_days", 3))
    ua = fetch.get("sec_user_agent", "Skyler M7 Tracker nosy.seo@gmail.com")

    symbols = get_ticker_symbols(cfg)
    if today is None:
        today = kst_today_date()

    # candidate 영업일들 (오늘 포함, 최신순)
    candidates = candidate_business_dates(today=today, max_back=walkback_max + 1, include_today=True)
    # walkback_max+1 → today 자체도 포함하므로 한계까지 시도

    found_text: Optional[str] = None
    file_date_used: Optional[date] = None
    walkback_days_used: int = 0

    for idx, cand in enumerate(candidates):
        yyyymmdd = cand.strftime("%Y%m%d")
        text = _fetch_finra_file(yyyymmdd, ua=ua)
        if text:
            found_text = text
            file_date_used = cand
            # walkback_days = today와의 영업일 차이
            walkback_days_used = idx
            break

    by_ticker: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    if not found_text or file_date_used is None:
        # 모든 walkback 실패
        msg = f"FINRA file not found in walkback window ({walkback_max} business days)"
        logger.error(msg)
        for sym in symbols:
            by_ticker[sym] = {
                "ticker": sym,
                "date": None,
                "short_volume": None,
                "total_volume": None,
                "short_ratio": None,
                "data_as_of": None,
                "freshness": "stale",
                "walkback_days": walkback_max,
                "error": msg,
            }
            errors[sym] = msg
        return {
            "by_ticker": by_ticker,
            "file_used": None,
            "walkback_days": walkback_max,
            "errors": errors,
            "freshness": "stale",
        }

    parsed = _parse_finra_text(found_text, symbols)
    iso_date = file_date_used.isoformat()
    # IMPR-031 패턴: walkback 안에 성공이면 fresh, 전부 실패해야 stale.
    # FINRA daily file은 T+1 publish가 정상이라 today walkback(0~1d)은 fresh.
    overall_freshness = "fresh"

    for sym in symbols:
        if sym in parsed:
            rec = parsed[sym]
            by_ticker[sym] = {
                "ticker": sym,
                "date": rec.get("date") or iso_date,
                "short_volume": rec["short_volume"],
                "total_volume": rec["total_volume"],
                "short_ratio": rec["short_ratio"],
                "data_as_of": iso_date,
                "freshness": overall_freshness,
                "walkback_days": walkback_days_used,
                "error": None,
            }
        else:
            msg = f"symbol {sym} not in FINRA CNMS file {iso_date}"
            errors[sym] = msg
            by_ticker[sym] = {
                "ticker": sym,
                "date": None,
                "short_volume": None,
                "total_volume": None,
                "short_ratio": None,
                "data_as_of": iso_date,
                "freshness": "stale",
                "walkback_days": walkback_days_used,
                "error": msg,
            }

    return {
        "by_ticker": by_ticker,
        "file_used": iso_date,
        "walkback_days": walkback_days_used,
        "errors": errors,
        "freshness": overall_freshness,
    }
