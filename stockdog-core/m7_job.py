"""
M7 Tracker Standalone Job (IMPR-044 Phase 1).

Runs alongside US market cron (Tue-Sat 17:00 KST = NYSE close window).
Fetches SEC EDGAR Form 4 (insider) + FINRA RegSHO daily (short) for M7 tickers.
Writes 3-tier raw artifacts (per-day dump + per-symbol history + per-symbol latest)
and optionally sends Telegram summary when threshold breaches detected.

Pattern: fear_greed_job.py 미러링 (--silent flag IMPR-038 패턴).
"""
import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict

from dotenv import load_dotenv

from analysis.m7_summary import summarize_m7
from collectors import m7_insider, m7_short
from utils.m7_config import load_m7_config, get_ticker_symbols
from utils.m7_store import (
    append_per_symbol,
    write_per_day_dump,
    write_per_symbol_latest,
)
from utils.notifier import send_telegram_message

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _today_str() -> str:
    """date_format YYYY-MM-DD (config 외부 의존 없음)."""
    return datetime.now().strftime("%Y-%m-%d")


def run(cfg: Dict[str, Any], silent: bool = False) -> int:
    """Returns exit code: 0 success, 1 all-fail."""
    if not cfg.get("enabled", True):
        print("[M7] enabled=false in m7.yaml — skip (noop).")
        return 0

    storage = cfg.get("storage", {}) or {}
    raw_base_dir = storage.get("raw_base_dir", "/notes/raw/stockdog/m7")
    history_cap = int(storage.get("per_symbol_history_cap_days", 90))
    schema_version = storage.get("schema_version", 1)

    date_str = _today_str()
    symbols = get_ticker_symbols(cfg)
    print(f"[M7] start date={date_str} symbols={symbols} raw_base_dir={raw_base_dir} silent={silent}")

    # 1. Collect
    print("[M7] fetching insider (SEC EDGAR Form 4)...")
    insider_result = m7_insider.collect_all(cfg)
    print(f"[M7] insider: filings_scanned={insider_result['filings_total']} "
          f"transactions={insider_result['transactions_total']} errors={list(insider_result['errors'].keys())}")

    print("[M7] fetching short volume (FINRA RegSHO CNMS)...")
    short_result = m7_short.collect_all(cfg)
    print(f"[M7] short: file_used={short_result['file_used']} walkback_days={short_result['walkback_days']} "
          f"freshness={short_result['freshness']} errors={list(short_result['errors'].keys())}")

    all_insider_failed = (
        len(insider_result.get("errors", {})) == len(symbols)
        and insider_result.get("transactions_total", 0) == 0
    )
    all_short_failed = short_result.get("file_used") is None

    # 2. Write 3-tier
    written_files = []
    try:
        per_day_insider_payload = {
            "schema_version": schema_version,
            "category": "insider",
            "date": date_str,
            "by_ticker": insider_result["by_ticker"],
            "errors": insider_result["errors"],
        }
        path = write_per_day_dump(raw_base_dir, "insider", date_str, per_day_insider_payload)
        written_files.append(path)
    except Exception as e:
        logger.error(f"per-day dump (insider) failed: {e}")

    try:
        per_day_short_payload = {
            "schema_version": schema_version,
            "category": "short",
            "date": date_str,
            "file_used": short_result.get("file_used"),
            "walkback_days": short_result.get("walkback_days"),
            "freshness": short_result.get("freshness"),
            "by_ticker": short_result["by_ticker"],
            "errors": short_result["errors"],
        }
        path = write_per_day_dump(raw_base_dir, "short", date_str, per_day_short_payload)
        written_files.append(path)
    except Exception as e:
        logger.error(f"per-day dump (short) failed: {e}")

    # per-symbol writes
    for symbol in symbols:
        # Insider
        try:
            insider_for_sym = insider_result["by_ticker"].get(symbol, {})
            insider_day_record = {
                "date": date_str,
                "schema_version": schema_version,
                "verified_cik": insider_for_sym.get("verified_cik"),
                "filings_scanned": insider_for_sym.get("filings_scanned", 0),
                "transactions": insider_for_sym.get("transactions", []),
                "error": insider_for_sym.get("error"),
            }
            p = append_per_symbol(raw_base_dir, "insider", symbol, insider_day_record, history_cap_days=history_cap)
            written_files.append(p)
            p = write_per_symbol_latest(raw_base_dir, "insider", symbol, insider_day_record)
            written_files.append(p)
        except Exception as e:
            logger.error(f"per-symbol insider write failed for {symbol}: {e}")

        # Short
        try:
            short_for_sym = short_result["by_ticker"].get(symbol, {})
            # 'data_as_of'가 있으면 그걸 date로 사용 (history에서 실제 거래일 기준이 더 자연스러움)
            short_record_date = short_for_sym.get("data_as_of") or date_str
            short_day_record = {
                "date": short_record_date,
                "schema_version": schema_version,
                "ticker": symbol,
                "short_volume": short_for_sym.get("short_volume"),
                "total_volume": short_for_sym.get("total_volume"),
                "short_ratio": short_for_sym.get("short_ratio"),
                "data_as_of": short_for_sym.get("data_as_of"),
                "freshness": short_for_sym.get("freshness"),
                "walkback_days": short_for_sym.get("walkback_days"),
                "error": short_for_sym.get("error"),
            }
            p = append_per_symbol(raw_base_dir, "short", symbol, short_day_record, history_cap_days=history_cap)
            written_files.append(p)
            p = write_per_symbol_latest(raw_base_dir, "short", symbol, short_day_record)
            written_files.append(p)
        except Exception as e:
            logger.error(f"per-symbol short write failed for {symbol}: {e}")

    print(f"[M7] wrote {len(written_files)} files (atomic).")

    # 3. Summary + optional Telegram
    summary_md = summarize_m7(date_str, cfg)

    # signal detection: summary contains the data rows (rough heuristic: contains pipe or 시그널)
    has_signal = ("|---|" in summary_md) and ("### Insider" in summary_md or "### 공매도" in summary_md)

    if silent:
        print("[M7] --silent — skip Telegram.")
    elif has_signal:
        # 첫 두 줄 + 시그널 요약 콜아웃을 압축 메시지로
        # Telegram 본문: 마크다운 H2/표 그대로 두지 말고 plain
        signal_line = ""
        for ln in summary_md.splitlines():
            if ln.startswith("> [!summary]"):
                signal_line = ln.replace("> [!summary]", "").strip()
                break
        msg = "🐺 *M7 Tracker* — 임계값 돌파\n\n"
        if signal_line:
            msg += signal_line
        else:
            msg += "Insider/Short 시그널 발화."
        msg += "\n🔗 https://blog.seosungho.com/trackers/m7"
        try:
            send_telegram_message(msg)
        except Exception as e:
            logger.warning(f"Telegram send failed (non-fatal): {e}")
    else:
        print("[M7] no breach signals → skip Telegram (silent-by-default per IMPR-038).")

    # exit code
    if all_insider_failed and all_short_failed:
        print("[M7] all-fail: insider + short both empty.")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="M7 Tracker (IMPR-044 Phase 1)")
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Skip Telegram notification (still writes JSON artifacts).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to m7.yaml (default: stockdog-core/config/m7.yaml)",
    )
    args = parser.parse_args()

    load_dotenv()
    try:
        cfg = load_m7_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"[M7] config error: {e}", file=sys.stderr)
        sys.exit(2)

    code = run(cfg, silent=args.silent)
    sys.exit(code)


if __name__ == "__main__":
    main()
