import os
from datetime import datetime
import logging
import requests

logger = logging.getLogger(__name__)


def _get_daily_dirs(config):
    """
    Returns (date_dir, media_dir) paths for today's date-based output folder.
    e.g. /notes/daily-market/2026-05-10/  and  /notes/daily-market/2026-05-10/media/
    """
    base_dir = config.get("obsidian", {}).get("base_dir", "/notes/daily-market")
    date_format = config.get("obsidian", {}).get("date_format", "%Y-%m-%d")
    current_date_str = datetime.now().strftime(date_format)

    date_dir = os.path.join(base_dir, current_date_str)
    media_dir = os.path.join(date_dir, "media")

    os.makedirs(date_dir, exist_ok=True)
    os.makedirs(media_dir, exist_ok=True)

    return date_dir, media_dir, current_date_str


def save_to_obsidian(content, config):
    """
    Wraps the LLM content with Obsidian frontmatter tags and saves it to the designated vault directory.
    Output: ~/daily-market/YYYY-MM-DD/Market_Report_YYYY-MM-DD.md
    """
    try:
        date_dir, _, current_date_str = _get_daily_dirs(config)

        filename = f"Market_Report_{current_date_str}.md"
        filepath = os.path.join(date_dir, filename)

        # Add metadata (frontmatter) for Obsidian
        frontmatter = f"---\ntags:\n  - market-report\n  - stockdog\ndate: {current_date_str}\n---\n\n"

        final_content = frontmatter + content

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(final_content)

        print(f"✅ Successfully saved Markdown report to {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to save Markdown file: {e}")
        return None


def save_raw_twitter_data(twitter_data, config):
    """
    Saves raw Twitter data as a Markdown file for transparency and record keeping.
    Output: ~/daily-market/YYYY-MM-DD/Raw_Twitter_YYYY-MM-DD.md
    Media:  ~/daily-market/YYYY-MM-DD/media/<tweet_images>
    """
    try:
        date_dir, media_dir, current_date_str = _get_daily_dirs(config)

        filename = f"Raw_Twitter_{current_date_str}.md"
        filepath = os.path.join(date_dir, filename)

        content = f"---\ntags:\n  - raw-data\n  - twitter\ndate: {current_date_str}\n---\n\n"
        content += f"# Raw Twitter Data Collection ({current_date_str})\n\n"

        for handle, tweets in twitter_data.items():
            content += f"## @{handle}\n"
            if not tweets:
                content += "No tweets collected or error fetching.\n\n"
                continue

            for tweet in tweets:
                content += f"- **Date**: {tweet.get('date', 'Unknown')}\n"
                content += f"- **Stats**: {tweet.get('likes', 0)} Likes, {tweet.get('retweets', 0)} Retweets\n"
                content += f"- **Content**:\n  > {tweet.get('content', '').replace(chr(10), chr(10) + '  > ')}\n"

                media_urls = tweet.get('media_urls', [])
                if media_urls:
                    for i, url in enumerate(media_urls):
                        try:
                            ext = url.split('.')[-1].split('?')[0]
                            if len(ext) > 4: ext = "jpg"
                            media_filename = f"{tweet.get('id', 'unknown')}_{i}.{ext}"
                            media_filepath = os.path.join(media_dir, media_filename)

                            if not os.path.exists(media_filepath):
                                r = requests.get(url, timeout=10)
                                if r.status_code == 200:
                                    with open(media_filepath, 'wb') as img_f:
                                        img_f.write(r.content)

                            content += f"  > \n  > ![[media/{media_filename}]]\n"
                        except Exception as img_e:
                            logger.error(f"Failed to download image {url}: {img_e}")

                content += "\n"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"✅ Successfully saved raw Twitter data to {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to save raw Twitter data: {e}")
        return None
