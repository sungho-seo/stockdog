"""
KR 데이터 fetch 공통 헬퍼.

금융위 (data.go.kr) API는 'basDt'를 영업일 기준으로 받는다.
- 장 마감(15:30 KST) 이후 ~ 자정 사이: 오늘 데이터가 들어와 있음 → 오늘이 우선
- 자정 ~ 장 마감 사이 / 공휴일 / 주말: 오늘 데이터 없음 → 어제로 fallback
- 둘 다 비면 (3일 연휴 등): None 반환, 호출부에서 빈 결과 + 경고로 graceful degrade

기존 _get_base_date()는 항상 어제만 반환해 1일 lag을 유발했다.
"""
from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger(__name__)


def kst_today_yyyymmdd() -> str:
    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return kst_now.strftime('%Y%m%d')


def kst_yesterday_yyyymmdd() -> str:
    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return (kst_now - timedelta(days=1)).strftime('%Y%m%d')


def candidate_base_dates() -> list:
    """오늘 → 어제 순서로 시도할 base_date 후보."""
    return [kst_today_yyyymmdd(), kst_yesterday_yyyymmdd()]


def try_fetch_with_fallback(fetch_fn, label: str):
    """
    fetch_fn(base_date) -> data dict | None.
    오늘 → 어제 순서로 시도하고 첫 성공 결과 반환.
    둘 다 None이면 (None, None) 반환. 호출부가 None 처리.

    Returns:
        (data, base_date_used) tuple, 둘 다 실패 시 (None, None).
    """
    for base_date in candidate_base_dates():
        try:
            data = fetch_fn(base_date)
        except Exception as e:
            logger.warning(f"[{label}] fetch error on {base_date}: {e}")
            data = None
        if data:
            logger.info(f"[{label}] base_date={base_date} OK")
            return data, base_date
        logger.debug(f"[{label}] no data on {base_date}, trying older date...")

    logger.warning(f"[{label}] no data on any candidate dates {candidate_base_dates()} (3일 연휴 또는 API 장애 가능)")
    return None, None
