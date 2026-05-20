"""
M7 트래커 config 로더 + per-ticker override merge 유틸.

config/m7.yaml schema (IMPR-044 P1-S2 lock):
  enabled: bool
  tickers: [{symbol, cik, overrides}]
  thresholds: {insider: {...}, short: {...}}
  fetch: {sec_user_agent, edgar_*, finra_*, form4_*}
  storage: {raw_base_dir, per_symbol_history_cap_days, schema_version}
  calendar: {source_module, source_constant}

overrides는 ticker별로 thresholds 필드 일부만 덮어쓰는 shallow per-bucket merge:
  ticker.overrides = {"insider": {"min_value_usd": 1000000}}
  → effective = {"insider": {**global.insider, "min_value_usd": 1000000},
                 "short": {**global.short}}
"""
import os
import logging
from typing import Dict, Any, Optional, List

import yaml

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "m7.yaml",
)


def load_m7_config(path: Optional[str] = None) -> Dict[str, Any]:
    """m7.yaml 로드 + 최소 schema 검증.

    schema 결함 발견 시 raise (호출부에서 graceful 처리).
    """
    config_path = path or DEFAULT_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 최소 schema 검증
    required_top = ("enabled", "tickers", "thresholds", "fetch", "storage")
    for key in required_top:
        if key not in cfg:
            raise ValueError(f"m7.yaml missing required key: {key!r}")

    if not isinstance(cfg["tickers"], list) or not cfg["tickers"]:
        raise ValueError("m7.yaml: tickers must be non-empty list")

    for t in cfg["tickers"]:
        if "symbol" not in t or "cik" not in t:
            raise ValueError(f"m7.yaml: ticker missing symbol/cik: {t!r}")
        t.setdefault("overrides", {})

    return cfg


def get_tickers(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """tickers list 반환 (symbol, cik, overrides keys 보장)."""
    return cfg.get("tickers", [])


def get_ticker_symbols(cfg: Dict[str, Any]) -> List[str]:
    return [t["symbol"] for t in get_tickers(cfg)]


def merge_thresholds(ticker_symbol: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """global thresholds + per-ticker override를 shallow per-bucket merge.

    Returns:
        {"insider": {...}, "short": {...}} — ticker-specific 유효 thresholds.

    Per-bucket shallow merge: bucket(=insider/short) 내부에서 override가 글로벌을 덮어씀.
    bucket 자체가 override에 없으면 global 그대로.
    """
    global_th = cfg.get("thresholds", {}) or {}
    ticker = next((t for t in get_tickers(cfg) if t["symbol"] == ticker_symbol), None)
    if not ticker:
        return {k: dict(v) for k, v in global_th.items()}

    overrides = ticker.get("overrides", {}) or {}
    merged: Dict[str, Dict[str, Any]] = {}
    # 모든 bucket(global + override 양쪽 합집합) 순회
    all_buckets = set(global_th.keys()) | set(overrides.keys())
    for bucket in all_buckets:
        merged[bucket] = {}
        if bucket in global_th and isinstance(global_th[bucket], dict):
            merged[bucket].update(global_th[bucket])
        if bucket in overrides and isinstance(overrides[bucket], dict):
            merged[bucket].update(overrides[bucket])
    return merged
