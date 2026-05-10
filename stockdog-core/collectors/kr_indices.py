import os
import requests
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

BASE_URL = "https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex"


def _get_base_date():
    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return (kst_now - timedelta(days=1)).strftime('%Y%m%d')


def fetch_kr_index(idx_name, api_key, base_date):
    """Fetches a single Korean index value from 금융위 지수 API."""
    params = {
        'serviceKey': api_key,
        'numOfRows': 1,
        'pageNo': 1,
        'resultType': 'json',
        'basDt': base_date,
        'idxNm': idx_name,
    }
    try:
        resp = requests.get(BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if not items:
            return None
        item = items[0] if isinstance(items, list) else items
        close = round(float(item.get('clpr', 0)), 2)
        prev_close = round(float(item.get('vs', 0)), 2)
        prev_close = round(close - prev_close, 2)
        change_pct = round(float(item.get('fltRt', 0)), 2)
        return {
            'close': close,
            'prev_close': prev_close,
            'change_pct': change_pct,
            'volume': int(item.get('trqu', 0)),
        }
    except Exception as e:
        logger.error(f"Failed to fetch KR index {idx_name}: {e}")
        return None


def get_kr_index_data(items):
    """
    items: list of {ticker (e.g. 'KOSPI'), name, type} — type: INDEX_KR
    Returns: {ticker: {name, type, close, prev_close, change_pct, volume}}
    """
    api_key = os.getenv('DATA_GO_KR_API_KEY')
    if not api_key:
        logger.error("DATA_GO_KR_API_KEY not set in .env")
        return {}

    base_date = _get_base_date()
    results = {}

    # Map watchlist ticker → API index name
    index_name_map = {
        'KOSPI': '코스피',
        'KOSDAQ': '코스닥',
    }

    for item in items:
        ticker = item['ticker']
        idx_name = index_name_map.get(ticker, ticker)
        print(f"Fetching KR index data for {idx_name}...")
        data = fetch_kr_index(idx_name, api_key, base_date)
        if data:
            results[ticker] = {
                'name': item['name'],
                'type': item['type'],
                **data,
            }
        else:
            logger.warning(f"No index data for {idx_name} on {base_date}")

    return results
