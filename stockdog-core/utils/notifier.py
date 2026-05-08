import os
import requests
import logging

logger = logging.getLogger(__name__)

def send_telegram_message(message: str):
    """
    Sends a text message to Telegram using BOT_TOKEN and CHAT_ID from env.
    """
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    
    if not token or not chat_id:
        logger.warning("Telegram BOT_TOKEN or CHAT_ID not set. Skipping notification.")
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram notification sent.")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")

def send_telegram_photo(photo_path: str, caption: str = None):
    """
    Sends a photo to Telegram.
    """
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    
    if not token or not chat_id:
        return
    
    if not os.path.exists(photo_path):
        logger.error(f"Photo path does not exist: {photo_path}")
        return

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    
    try:
        with open(photo_path, "rb") as photo:
            files = {"photo": photo}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            
            resp = requests.post(url, data=data, files=files, timeout=30)
            resp.raise_for_status()
            logger.info("Telegram photo sent.")
    except Exception as e:
        logger.error(f"Failed to send Telegram photo: {e}")
