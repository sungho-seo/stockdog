import logging

logger = logging.getLogger(__name__)


def read_influencers(file_path):
    """
    Parses _system/influencers.md table.
    Returns handles where 활성 column = ✅.
    Table format: | handle | name | 성향 | 활성 |
    """
    handles = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("|") or "---" in line or "핸들" in line:
                    continue
                cols = [c.strip() for c in line.strip("|").split("|")]
                if len(cols) >= 4 and cols[3] == "✅":
                    handles.append(cols[0])
    except FileNotFoundError:
        logger.error(f"Influencers file not found: {file_path}")
    return handles


def read_watchlist_tickers(file_path, types=("STOCK",)):
    """
    Parses _system/watchlist.md.
    Returns tickers matching the given TYPE values.
    Line format: TICKER|Full Name|TYPE  (pipe-separated, not a markdown table)
    """
    tickers = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith(">") or "|" not in line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 3 and parts[2] in types:
                    tickers.append(parts[0])
    except FileNotFoundError:
        logger.error(f"Watchlist file not found: {file_path}")
    return tickers
