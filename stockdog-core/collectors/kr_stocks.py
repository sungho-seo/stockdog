import os
import requests
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

BASE_URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"


def _get_base_date():
    """Returns yesterday's date in KST as YYYYMMDD string."""
    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return (kst_now - timedelta(days=1)).strftime('%Y%m%d')


def fetch_kr_stock(srtn_cd, api_key, base_date):
    """Fetches a single Korean stock's price from 금융위 API."""
    params = {
        'serviceKey': api_key,
        'numOfRows': 1,
        'pageNo': 1,
        'resultType': 'json',
        'basDt': base_date,
        'srtnCd': srtn_cd,
    }
    try:
        resp = requests.get(BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if not items:
            return None
        item = items[0] if isinstance(items, list) else items
        close = int(item.get('clpr', 0))
        prev_close = close - int(item.get('vs', 0))
        change_pct = round(float(item.get('fltRt', 0)), 2)
        return {
            'close': close,
            'prev_close': prev_close,
            'change_pct': change_pct,
            'volume': int(item.get('trqu', 0)),
            'market': item.get('mrktCtg', ''),
        }
    except Exception as e:
        logger.error(f"Failed to fetch KR stock {srtn_cd}: {e}")
        return None


def get_kr_stock_data(items):
    """
    items: list of {ticker (6-digit code), name, type} — types: STOCK_KR, ETF_KR
    Returns: {ticker: {name, type, close, prev_close, change_pct, volume, market}}
    """
    api_key = os.getenv('DATA_GO_KR_API_KEY')
    if not api_key:
        logger.error("DATA_GO_KR_API_KEY not set in .env")
        return {}

    base_date = _get_base_date()
    results = {}

    for item in items:
        ticker = item['ticker']
        print(f"Fetching KR stock data for {ticker} ({item['name']})...")
        data = fetch_kr_stock(ticker, api_key, base_date)
        if data:
            results[ticker] = {
                'name': item['name'],
                'type': item['type'],
                **data,
            }
        else:
            logger.warning(f"No data for {ticker} on {base_date} (holiday or weekend?)")

    return results
