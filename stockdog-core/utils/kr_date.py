"""
KR 데이터 fetch 공통 헬퍼.

금융위 (data.go.kr) API는 'basDt'를 영업일 기준으로 받는다.
- 장 마감(15:30 KST) 이후 ~ 자정 사이: 오늘 데이터가 들어와 있음 → 오늘이 우선
- 자정 ~ 장 마감 사이 / 공휴일 / 주말: 오늘 데이터 없음 → 직전 영업일로 fallback
- N=5 영업일까지 모두 비면: None 반환, 호출부에서 graceful degrade

IMPR-031: fallback window를 1일 → 5영업일로 확장 + 한국 공휴일·주말 자동 skip.
"""
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Set
import logging

from utils.kr_holidays import KR_HOLIDAYS_2026

logger = logging.getLogger(__name__)


def kst_today_yyyymmdd() -> str:
    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return kst_now.strftime('%Y%m%d')


def kst_yesterday_yyyymmdd() -> str:
    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return (kst_now - timedelta(days=1)).strftime('%Y%m%d')


def _kst_today_date() -> date:
    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return kst_now.date()


def _is_business_day(d: date, holidays: Set[str]) -> bool:
    """월~금 + 공휴일 set에 없음 → 영업일."""
    if d.weekday() >= 5:  # 5=토, 6=일
        return False
    return d.strftime('%Y%m%d') not in holidays


def candidate_base_dates(
    today: Optional[date] = None,
    max_back: int = 5,
    holidays: Optional[Set[str]] = None,
) -> list:
    """today부터 backward로 영업일만 yyyymmdd 리스트로 반환 (최대 max_back개).

    토/일/한국 공휴일(holidays) 자동 skip. today 자체도 영업일이면 첫 후보로 포함.
    """
    if today is None:
        today = _kst_today_date()
    if holidays is None:
        holidays = KR_HOLIDAYS_2026

    result = []
    cur = today
    # 최악의 경우 연휴+주말로 7~9일 backward 필요. 안전 margin으로 max_back*4.
    safety_cap = max_back * 4 + 7
    steps = 0
    while len(result) < max_back and steps < safety_cap:
        if _is_business_day(cur, holidays):
            result.append(cur.strftime('%Y%m%d'))
        cur = cur - timedelta(days=1)
        steps += 1
    return result


def business_days_between(
    d1: date,
    d2: date,
    holidays: Optional[Set[str]] = None,
) -> int:
    """d1 ~ d2 사이의 영업일 개수 (양 끝 포함 차이, 부호 절댓값).

    d1=d2 → 0. d1이 영업일이고 d2가 직전 영업일이면 1. 비영업일 → 영업일 차로 환산.
    """
    if holidays is None:
        holidays = KR_HOLIDAYS_2026
    if d1 == d2:
        return 0
    start, end = (d2, d1) if d2 < d1 else (d1, d2)
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if _is_business_day(cur, holidays):
            count += 1
        cur += timedelta(days=1)
    return count


def try_fetch_with_fallback(fetch_fn, label: str):
    """
    fetch_fn(base_date) -> data dict | None.
    오늘 → 과거 영업일 순서로 시도하고 첫 성공 결과 반환.
    모두 None이면 (None, None) 반환. 호출부가 None 처리.

    Returns:
        (data, base_date_used) tuple, 모두 실패 시 (None, None).
    """
    candidates = candidate_base_dates()
    for base_date in candidates:
        try:
            data = fetch_fn(base_date)
        except Exception as e:
            logger.warning(f"[{label}] fetch error on {base_date}: {e}")
            data = None
        if data:
            logger.info(f"[{label}] base_date={base_date} OK")
            return data, base_date
        logger.debug(f"[{label}] no data on {base_date}, trying older date...")

    logger.warning(f"[{label}] no data on any candidate dates {candidates} (장기 휴장 또는 API 장애 가능)")
    return None, None
