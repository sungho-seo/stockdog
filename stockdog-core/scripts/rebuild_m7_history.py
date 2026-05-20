"""
One-shot recovery: rebuild M7 per-symbol history files from per-day dumps.

Use case:
  - schema_version 변경 후 history file을 새 스키마로 재생성하고 싶을 때
  - per-symbol history file이 깨졌거나 삭제됐을 때
  - manually-edited per-day dump을 정식 history에 반영하고 싶을 때

Flow:
  1. {raw_base_dir}/{category}/*.json glob → per-day dumps 모두 로드
  2. 각 dump의 by_ticker[symbol]에서 day_record 재구성
  3. 날짜순 정렬 + history_cap_days cap 적용 후 per-symbol history JSON으로 atomic write

cron 외, 수동 호출 전용.
"""
import argparse
import glob
import json
import logging
import os
import sys
from typing import Any, Dict, List

# Allow `python scripts/rebuild_m7_history.py` from stockdog-core root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.m7_config import load_m7_config, get_ticker_symbols  # noqa: E402
from utils.m7_store import _atomic_write_json  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _load_per_day_dumps(raw_base_dir: str, category: str) -> List[Dict[str, Any]]:
    pattern = os.path.join(raw_base_dir, category, "*.json")
    dumps = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                dumps.append(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Skip corrupted dump {path}: {e}")
    return dumps


def rebuild_category(raw_base_dir: str, category: str, symbols: List[str], history_cap_days: int) -> Dict[str, int]:
    """Returns {symbol: written_record_count}."""
    dumps = _load_per_day_dumps(raw_base_dir, category)
    print(f"[rebuild] category={category} loaded {len(dumps)} per-day dumps")

    per_symbol: Dict[str, List[Dict[str, Any]]] = {sym: [] for sym in symbols}

    for dump in dumps:
        date_str = dump.get("date")
        by_ticker = dump.get("by_ticker") or {}
        for sym in symbols:
            sym_data = by_ticker.get(sym)
            if not sym_data:
                continue
            # day_record 재구성 — m7_job.py와 동일 schema 유지
            if category == "insider":
                day_record = {
                    "date": date_str,
                    "schema_version": dump.get("schema_version"),
                    "verified_cik": sym_data.get("verified_cik"),
                    "filings_scanned": sym_data.get("filings_scanned", 0),
                    "transactions": sym_data.get("transactions", []),
                    "error": sym_data.get("error"),
                }
            else:  # short
                day_record = {
                    "date": sym_data.get("data_as_of") or date_str,
                    "schema_version": dump.get("schema_version"),
                    "ticker": sym,
                    "short_volume": sym_data.get("short_volume"),
                    "total_volume": sym_data.get("total_volume"),
                    "short_ratio": sym_data.get("short_ratio"),
                    "data_as_of": sym_data.get("data_as_of"),
                    "freshness": sym_data.get("freshness"),
                    "walkback_days": sym_data.get("walkback_days"),
                    "error": sym_data.get("error"),
                }
            per_symbol[sym].append(day_record)

    counts: Dict[str, int] = {}
    for sym, records in per_symbol.items():
        # dedupe by date (keep latest write — assume sorted dump order is chronological)
        seen = {}
        for r in records:
            d = r.get("date")
            if not d:
                continue
            seen[d] = r  # later wins
        unique = list(seen.values())
        unique.sort(key=lambda r: r.get("date", ""), reverse=True)
        if history_cap_days and len(unique) > history_cap_days:
            unique = unique[:history_cap_days]
        out_path = os.path.join(raw_base_dir, sym, f"{category}_history.json")
        _atomic_write_json(out_path, unique)
        counts[sym] = len(unique)
        print(f"  {sym}/{category}_history.json ← {len(unique)} records")
    return counts


def main():
    parser = argparse.ArgumentParser(description="Rebuild M7 per-symbol history from per-day dumps.")
    parser.add_argument("--config", default=None, help="Path to m7.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Don't write — just print summary")
    parser.add_argument(
        "--category",
        choices=["insider", "short", "both"],
        default="both",
        help="Which category to rebuild (default: both)",
    )
    args = parser.parse_args()

    cfg = load_m7_config(args.config)
    storage = cfg.get("storage", {}) or {}
    raw_base_dir = storage.get("raw_base_dir", "/notes/raw/stockdog/m7")
    history_cap = int(storage.get("per_symbol_history_cap_days", 90))
    symbols = get_ticker_symbols(cfg)

    if args.dry_run:
        print(f"[DRY-RUN] would rebuild category={args.category} symbols={symbols} cap={history_cap}d")
        return

    categories = ["insider", "short"] if args.category == "both" else [args.category]
    for cat in categories:
        rebuild_category(raw_base_dir, cat, symbols, history_cap)

    print("[rebuild] done.")


if __name__ == "__main__":
    main()
