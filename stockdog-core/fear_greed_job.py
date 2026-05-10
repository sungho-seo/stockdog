"""
Fear & Greed Index Standalone Job
Runs at US market open (9:30 AM ET) via crontab.
Fetches the index, generates the gauge chart, and sends it via Telegram.
"""
import os
import yaml
import logging
from datetime import datetime
from dotenv import load_dotenv

from collectors.market_indicators import fetch_fear_and_greed
from utils.chart_generator import generate_fear_greed_gauge
from utils.markdown_generator import _get_daily_dirs
from utils.notifier import send_telegram_message, send_telegram_photo

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def main():
    print("📊 Fear & Greed Job: Fetching at US market open...")
    
    load_dotenv()
    config = load_config()
    
    # Fetch F&G
    fgi = fetch_fear_and_greed()
    score = fgi.get("score")
    rating = fgi.get("rating", "unknown")
    
    if score is None:
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
    
    # Send to Telegram
    summary = f"📊 *Fear & Greed Index* (US Market Open)\n\nScore: *{int(round(score))}* ({rating.upper()})"
    
    if gauge_path:
        send_telegram_photo(gauge_path, caption=summary)
    else:
        send_telegram_message(summary)
    
    print("📊 Fear & Greed Job complete.")

if __name__ == "__main__":
    main()
