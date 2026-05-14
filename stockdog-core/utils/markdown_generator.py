import os
from datetime import datetime
import logging
from typing import Optional
import requests

logger = logging.getLogger(__name__)


def _get_daily_dirs(config):
    """
    Returns (date_dir, media_dir, date_str) for today's output folder.
    e.g. /notes/daily-market/2026-05-10/ and /notes/daily-market/2026-05-10/media/
    """
    base_dir = config.get("obsidian", {}).get("base_dir", "/notes/daily-market")
    date_format = config.get("obsidian", {}).get("date_format", "%Y-%m-%d")
    date_str = datetime.now().strftime(date_format)

    date_dir = os.path.join(base_dir, date_str)
    media_dir = os.path.join(date_dir, "media")

    os.makedirs(date_dir, exist_ok=True)
    os.makedirs(media_dir, exist_ok=True)

    return date_dir, media_dir, date_str


VALID_STATUSES = {"complete", "partial", "failed"}


def save_report(content, config, region="US", status="complete", data_as_of: Optional[str] = None):
    """
    Saves an LLM-generated report with Obsidian frontmatter.
    region: 'US' or 'KR'
    status: 'complete' | 'partial' | 'failed' — brief/quality 측 분기용.
        complete: 핵심 데이터 모두 수신
        partial : 일부 필드 N/A (13F·influencer quiet 등 normal 케이스 포함)
        failed  : 핵심 데이터 부재 (예: KR에서 KOSPI/KOSDAQ 둘 다 N/A)
    data_as_of: 실제 거래일(YYYY-MM-DD). None이면 frontmatter에 라인 생략.
        `date`는 작성일(KST 실행일), `data_as_of`는 본문 시장 데이터의 실제 거래일.
        KR cron은 data.go.kr 한계로 D-1 데이터를 받으므로 두 값이 다를 수 있다.
    Output: /notes/daily-market/YYYY-MM-DD/Market_Report_{region}_YYYY-MM-DD.md
    """
    if status not in VALID_STATUSES:
        logger.warning(f"Invalid status {status!r}, defaulting to 'complete'")
        status = "complete"

    try:
        date_dir, _, date_str = _get_daily_dirs(config)
        filename = f"Market_Report_{region}_{date_str}.md"
        filepath = os.path.join(date_dir, filename)

        tag = "market-report-us" if region == "US" else "market-report-kr"
        data_as_of_line = f"data_as_of: {data_as_of}\n" if data_as_of else ""
        frontmatter = (
            "---\n"
            f"tags:\n  - {tag}\n  - stockdog\n"
            f"date: {date_str}\n"
            f"{data_as_of_line}"
            f"status: {status}\n"
            "---\n\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(frontmatter + content)

        print(f"✅ Saved {filename} (status={status}, data_as_of={data_as_of or 'N/A'})")
        return filepath
    except Exception as e:
        logger.error(f"Failed to save {region} report: {e}")
        return None


def save_raw_twitter_data(twitter_data, config):
    """
    Saves raw Twitter data as Markdown.
    Output: /notes/daily-market/YYYY-MM-DD/Raw_Twitter_YYYY-MM-DD.md
    """
    try:
        date_dir, media_dir, date_str = _get_daily_dirs(config)
        filename = f"Raw_Twitter_{date_str}.md"
        filepath = os.path.join(date_dir, filename)

        content = f"---\ntags:\n  - raw-data\n  - twitter\ndate: {date_str}\n---\n\n"
        content += f"# Raw Twitter Data ({date_str})\n\n"

        for handle, tweets in twitter_data.items():
            content += f"## @{handle}\n"
            if not tweets:
                content += "No tweets collected.\n\n"
                continue

            for tweet in tweets:
                content += f"- **Date**: {tweet.get('date', 'Unknown')}\n"
                content += f"- **Stats**: {tweet.get('likes', 0)} Likes, {tweet.get('retweets', 0)} Retweets\n"
                content += f"- **Content**:\n  > {tweet.get('content', '').replace(chr(10), chr(10) + '  > ')}\n"

                for i, url in enumerate(tweet.get('media_urls', [])):
                    try:
                        ext = url.split('.')[-1].split('?')[0]
                        if len(ext) > 4:
                            ext = "jpg"
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

        print(f"✅ Saved {filename}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to save raw Twitter data: {e}")
        return None
