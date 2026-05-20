"""
US (NYSE) 데이터 fetch 공통 헬퍼.

FINRA RegSHO daily file은 NYSE 영업일 기준 T-1. 휴장일/주말은 파일 자체가 없음.
- US 오전 (KST 오후): 오늘 file 없을 수 있음 → 어제 / 그제 walkback
- 휴장일(NYSE_HOLIDAYS_2026)은 walkback에 카운트 안 함, 자동 skip

kr_date.py 패턴 미러링 + NYSE holiday set 교체. ISO 날짜("YYYY-MM-DD") 우선 사용.
"""
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Set, List
import logging

from utils.us_holidays import NYSE_HOLIDAYS_2026

logger = logging.getLogger(__name__)


def kst_today_date() -> date:
    """KST 기준 오늘 date 객체."""
    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return kst_now.date()


def _is_business_day(d: date, holidays: Set[str]) -> bool:
    """월~금 + NYSE 휴장일 set에 없음 → 영업일."""
    if d.weekday() >= 5:  # 5=토, 6=일
        return False
    return d.isoformat() not in holidays


def previous_business_day(
    d: date,
    max_walkback_days: int = 5,
    holidays: Optional[Set[str]] = None,
) -> Optional[date]:
    """d 직전 영업일 반환. max_walkback_days 안에 못 찾으면 None.

    d 자체는 후보 아님 — 이전 영업일을 찾음. 토/일/NYSE 휴장일 자동 skip.
    """
    if holidays is None:
        holidays = NYSE_HOLIDAYS_2026

    # 휴장일/주말이 연속될 수 있어 안전 마진. max_walkback_days 영업일 찾으려면
    # 캘린더 일수로는 더 많이 거슬러야 함.
    safety_cap = max_walkback_days * 4 + 7
    cur = d - timedelta(days=1)
    steps = 0
    while steps < safety_cap:
        if _is_business_day(cur, holidays):
            return cur
        cur = cur - timedelta(days=1)
        steps += 1
    return None


def candidate_business_dates(
    today: Optional[date] = None,
    max_back: int = 3,
    holidays: Optional[Set[str]] = None,
    include_today: bool = True,
) -> List[date]:
    """today부터 backward로 영업일만 date 리스트로 반환 (최대 max_back개).

    토/일/NYSE 휴장일 자동 skip. include_today=False면 today는 제외하고 직전 영업일부터.
    """
    if today is None:
        today = kst_today_date()
    if holidays is None:
        holidays = NYSE_HOLIDAYS_2026

    result: List[date] = []
    cur = today if include_today else today - timedelta(days=1)
    safety_cap = max_back * 4 + 7
    steps = 0
    while len(result) < max_back and steps < safety_cap:
        if _is_business_day(cur, holidays):
            result.append(cur)
        cur = cur - timedelta(days=1)
        steps += 1
    return result
