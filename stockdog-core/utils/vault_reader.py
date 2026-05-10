import logging

logger = logging.getLogger(__name__)


def read_influencers(file_path):
    """
    Parses _system/influencers.md table.
    Returns list of handles where 활성 column = ✅.
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


def read_watchlist_items(file_path, types=None):
    """
    Parses _system/watchlist.md.
    Line format: TICKER|Full Name|TYPE[|extra]
    types: tuple/list of TYPE strings to filter, None = all
    Returns: list of {ticker, name, type, extra}
    """
    items = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith(">") or "|" not in line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3:
                    continue
                item_type = parts[2]
                if types is None or item_type in types:
                    items.append({
                        'ticker': parts[0],
                        'name': parts[1],
                        'type': item_type,
                        'extra': parts[3] if len(parts) > 3 else None,
                    })
    except FileNotFoundError:
        logger.error(f"Watchlist file not found: {file_path}")
    return items
