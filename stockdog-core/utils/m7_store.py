"""
M7 트래커 raw 저장 — 3-write 패턴.

저장 구조 (storage.raw_base_dir = /notes/raw/stockdog/m7):
  m7/{category}/{date_str}.json           — per-day dump, 7-symbol 묶음 (audit trail, 무제한 보존)
  m7/{ticker}/{category}_history.json     — per-symbol 시계열 (날짜 내림차순 list, 90일 cap)
  m7/{ticker}/{category}_latest.json      — per-symbol 최신 단일 레코드 (Phase 3 widget fetch 대상)

category: "insider" | "short"
date_str: "YYYY-MM-DD"

모두 atomic write (tmp + os.replace, fear_greed_job.py:62-67 패턴).
디렉토리 없으면 mkdir. IO 실패는 raise — 호출부에서 try/except.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _atomic_write_json(path: str, payload: Any) -> None:
    """tmp + os.replace로 atomic write. 부모 디렉토리 자동 생성."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json_safe(path: str) -> Optional[Any]:
    """파일 없거나 깨졌으면 None. 정상 read는 dict/list 그대로."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"m7_store: corrupted/unreadable {path}: {e}")
        return None


def write_per_day_dump(raw_base_dir: str, category: str, date_str: str, payload: Dict[str, Any]) -> str:
    """7-symbol 묶음 audit trail.

    Output: {raw_base_dir}/{category}/{date_str}.json
    Returns: 저장 경로.
    """
    path = os.path.join(raw_base_dir, category, f"{date_str}.json")
    _atomic_write_json(path, payload)
    logger.info(f"m7_store: per-day dump → {path}")
    return path


def append_per_symbol(
    raw_base_dir: str,
    category: str,
    ticker: str,
    day_record: Dict[str, Any],
    history_cap_days: int = 90,
) -> str:
    """per-symbol 시계열 history에 day_record 추가.

    - 기존 history 읽어서 같은 날짜 있으면 교체, 없으면 prepend (날짜 내림차순)
    - history_cap_days 초과 시 오래된 것부터 잘라냄
    - day_record는 'date' 필드를 포함해야 함 (YYYY-MM-DD)

    Output: {raw_base_dir}/{ticker}/{category}_history.json
    Returns: 저장 경로.
    """
    if "date" not in day_record:
        raise ValueError(f"m7_store.append_per_symbol: day_record requires 'date' field, got {day_record!r}")

    path = os.path.join(raw_base_dir, ticker, f"{category}_history.json")
    existing = _read_json_safe(path)
    history: List[Dict[str, Any]] = existing if isinstance(existing, list) else []

    # 같은 날짜 제거(중복 방지) 후 prepend
    target_date = day_record["date"]
    history = [r for r in history if r.get("date") != target_date]
    history.insert(0, day_record)

    # 날짜 내림차순 정렬 보장 (방어적)
    history.sort(key=lambda r: r.get("date", ""), reverse=True)

    # cap 적용
    if history_cap_days and len(history) > history_cap_days:
        history = history[:history_cap_days]

    _atomic_write_json(path, history)
    logger.info(f"m7_store: per-symbol history ({ticker}/{category}) → {path} [{len(history)} records]")
    return path


def write_per_symbol_latest(
    raw_base_dir: str,
    category: str,
    ticker: str,
    latest_record: Dict[str, Any],
) -> str:
    """per-symbol 최신 단일 레코드 (Phase 3 widget fetch 대상).

    Output: {raw_base_dir}/{ticker}/{category}_latest.json
    Returns: 저장 경로.
    """
    path = os.path.join(raw_base_dir, ticker, f"{category}_latest.json")
    _atomic_write_json(path, latest_record)
    logger.info(f"m7_store: per-symbol latest ({ticker}/{category}) → {path}")
    return path


def read_per_symbol_history(raw_base_dir: str, category: str, ticker: str) -> List[Dict[str, Any]]:
    """per-symbol history 읽기 (없으면 []). MA 계산용."""
    path = os.path.join(raw_base_dir, ticker, f"{category}_history.json")
    data = _read_json_safe(path)
    return data if isinstance(data, list) else []


def read_per_symbol_latest(raw_base_dir: str, category: str, ticker: str) -> Optional[Dict[str, Any]]:
    """per-symbol latest 단일 레코드 (없으면 None)."""
    path = os.path.join(raw_base_dir, ticker, f"{category}_latest.json")
    data = _read_json_safe(path)
    return data if isinstance(data, dict) else None
