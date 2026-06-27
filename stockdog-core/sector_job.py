"""
US Sector Rotation Tracker Standalone Job.

Fetches 11 GICS sector ETFs, computes rotation metrics, and writes snapshot.
Mirrors m7_job.py pattern: --silent flag, atomic write, tolerant (partial → write what we have).

Pattern: fear_greed_job.py + m7_job.py (--silent flag IMPR-038).
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict

from dotenv import load_dotenv

from collectors.us_sectors import collect_us_sectors
from utils.notifier import send_telegram_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _atomic_write_json(path: str, payload: Any) -> None:
    """Atomic write: tmp + os.replace. Creates parent dirs."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def run(silent: bool = False) -> int:
    """
    Collect sector data and write snapshot.

    Returns: 0 success, 1 failure.
    """
    print("[Sectors] start")

    # Hard-coded path (matches m7_job pattern). In container: /notes/raw/stockdog/sectors
    raw_base_dir = "/notes/raw/stockdog/sectors"

    # 1. Collect
    print("[Sectors] collecting US sector ETF data...")
    try:
        payload = collect_us_sectors()
    except Exception as e:
        logger.error(f"[Sectors] collection failed: {e}")
        if not silent:
            try:
                send_telegram_message(f"⚠️ Sector Rotation Tracker — collection failed: {e}")
            except Exception as te:
                logger.warning(f"Telegram send failed (non-fatal): {te}")
        return 1

    # 2. Write snapshot (atomic)
    snapshot_path = os.path.join(raw_base_dir, "sectors_snapshot.json")
    try:
        _atomic_write_json(snapshot_path, payload)
        print(f"[Sectors] wrote snapshot: {snapshot_path}")
    except Exception as e:
        logger.error(f"[Sectors] write failed: {e}")
        if not silent:
            try:
                send_telegram_message(f"⚠️ Sector Rotation Tracker — write failed: {e}")
            except Exception as te:
                logger.warning(f"Telegram send failed (non-fatal): {te}")
        return 1

    # 3. Optional Telegram notification
    if silent:
        print("[Sectors] --silent — skip Telegram.")
    else:
        try:
            asof = payload.get("asof", "unknown")
            sectors_valid = [s for s in payload.get("sectors", []) if s.get("momentum") is not None]
            top3 = sorted(sectors_valid, key=lambda s: s["momentum"], reverse=True)[:3]
            bottom1 = sorted(sectors_valid, key=lambda s: s["momentum"])[:1]

            lines = [f"📊 *Sector Rotation Tracker* — {asof}\n"]
            if top3:
                lines.append("🔝 Top 3 by momentum:")
                for s in top3:
                    lines.append(f"  {s['etf']} ({s['name']}) +{s['momentum']:.2f}")
            if bottom1:
                lines.append("📉 Bottom 1:")
                for s in bottom1:
                    lines.append(f"  {s['etf']} ({s['name']}) {s['momentum']:.2f}")

            msg = "\n".join(lines)
            send_telegram_message(msg)
        except Exception as e:
            logger.warning(f"Telegram send failed (non-fatal): {e}")

    print("[Sectors] complete.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="US Sector Rotation Tracker Job")
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Skip Telegram notification (still writes JSON snapshot).",
    )
    args = parser.parse_args()

    load_dotenv()
    code = run(silent=args.silent)
    sys.exit(code)


if __name__ == "__main__":
    main()
