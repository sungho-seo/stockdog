import os
import requests
import logging

from utils.kr_date import try_fetch_with_fallback

logger = logging.getLogger(__name__)

BASE_URL = "https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex"


def _parse_index_item(item):
    """API 응답의 단일 item dict → 표준 지수 dict."""
    close = round(float(item.get('clpr', 0)), 2)
    vs = round(float(item.get('vs', 0)), 2)
    prev_close = round(close - vs, 2)
    change_pct = round(float(item.get('fltRt', 0)), 2)
    return {
        'close': close,
        'prev_close': prev_close,
        'change_pct': change_pct,
        'volume': int(item.get('trqu', 0)),
    }


def _request_index(idx_name, api_key, base_date):
    params = {
        'serviceKey': api_key,
        'numOfRows': 1,
        'pageNo': 1,
        'resultType': 'json',
        'basDt': base_date,
        'idxNm': idx_name,
    }
    resp = requests.get(BASE_URL, params=params, timeout=10)
    resp.raise_for_status()
    items = resp.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
    if not items:
        return None
    item = items[0] if isinstance(items, list) else items
    return _parse_index_item(item)


def fetch_kr_index(idx_name, api_key, base_date=None):
    """단일 지수 fetch.

    base_date=None이면 오늘 → 어제 fallback, (data, base_date_used) 반환.
    base_date가 지정되면 단일 호출 후 data만 반환(테스트용 기존 호환).
    """
    if base_date is not None:
        try:
            return _request_index(idx_name, api_key, base_date)
        except Exception as e:
            logger.error(f"Failed to fetch KR index {idx_name} on {base_date}: {e}")
            return None

    def _do(bd):
        return _request_index(idx_name, api_key, bd)

    data, base_date_used = try_fetch_with_fallback(_do, label=f"kr_index {idx_name}")
    return data, base_date_used


def get_kr_index_data(items):
    """
    items: list of {ticker (e.g. 'KOSPI'), name, type} — type: INDEX_KR
    Returns: {ticker: {name, type, close, prev_close, change_pct, volume,
                       base_date: 'YYYYMMDD' | None}}
    각 결과에 실제 사용된 base_date를 함께 넣어 pipeline이 frontmatter
    data_as_of로 노출할 수 있게 한다.
    """
    api_key = os.getenv('DATA_GO_KR_API_KEY')
    if not api_key:
        logger.error("DATA_GO_KR_API_KEY not set in .env")
        return {}

    # Map watchlist ticker → API index name
    index_name_map = {
        'KOSPI': '코스피',
        'KOSDAQ': '코스닥',
    }

    results = {}
    for item in items:
        ticker = item['ticker']
        idx_name = index_name_map.get(ticker, ticker)
        print(f"Fetching KR index data for {idx_name}...")
        data, base_date_used = fetch_kr_index(idx_name, api_key)
        if data:
            results[ticker] = {
                'name': item['name'],
                'type': item['type'],
                'base_date': base_date_used,
                **data,
            }
        else:
            logger.warning(f"No index data for {idx_name} (오늘·어제 모두 실패)")

    return results
