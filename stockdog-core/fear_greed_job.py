"""
Fear & Greed Index Standalone Job
Runs at US market open (9:30 AM ET) via crontab.
Fetches the index, generates the gauge chart, and sends it via Telegram.
"""
import os
import json
import yaml
import logging
import argparse
from datetime import datetime
from dotenv import load_dotenv

from collectors.market_indicators import fetch_fear_and_greed_full
from utils.chart_generator import generate_fear_greed_gauge
from utils.markdown_generator import _get_daily_dirs
from utils.notifier import send_telegram_message, send_telegram_photo

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def main(silent: bool = False):
    print(f"📊 Fear & Greed Job: silent={silent}")

    load_dotenv()
    config = load_config()

    # Fetch F&G (single HTTP round trip — raw graphdata)
    raw_data = fetch_fear_and_greed_full()
    if raw_data:
        fg = raw_data.get("fear_and_greed", {})
        raw_score = fg.get("score")
        rating = fg.get("rating", "unknown")
        score = round(raw_score) if raw_score is not None else None
    else:
        score = None
        rating = "unknown"

    if score is None:
        if not silent:
            send_telegram_message("⚠️ Fear & Greed 지수를 가져올 수 없습니다.")
        print("❌ Failed to fetch Fear & Greed Index.")
        return

    print(f"✅ Score: {score}, Rating: {rating}")

    # Generate gauge image into today's media/ folder
    _, media_dir, date_str = _get_daily_dirs(config)
    gauge_path = generate_fear_greed_gauge(
        score=score,
        rating=rating,
        output_dir=media_dir
    )

    # Dump full graphdata JSON next to PNG/SVG (atomic write).
    # Must NOT block PNG/Telegram path on failure.
    json_path = os.path.join(media_dir, "fear_greed.json")
    try:
        if raw_data:
            tmp = json_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw_data, f, ensure_ascii=False)
            os.replace(tmp, json_path)
            print(f"📁 F&G JSON dumped to {json_path}")
    except Exception as e:
        logger.warning(f"F&G JSON dump failed: {e}")

    # Send to Telegram (skip if silent)
    if silent:
        print("🔇 silent mode — skipping Telegram")
    else:
        summary = f"📊 *Fear & Greed Index* (US Market Open)\n\nScore: *{int(round(score))}* ({rating.upper()})"
        if gauge_path:
            send_telegram_photo(gauge_path, caption=summary)
        else:
            send_telegram_message(summary)

    print("📊 Fear & Greed Job complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--silent", action="store_true", help="Skip Telegram notification (still writes PNG/JSON).")
    args = parser.parse_args()
    main(silent=args.silent)
