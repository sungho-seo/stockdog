import os
import requests
import logging

from utils.kr_date import try_fetch_with_fallback

logger = logging.getLogger(__name__)

BASE_URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"


def _parse_stock_item(item):
    """API 응답의 단일 item dict → 표준 가격 dict."""
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


def _request_stock(srtn_cd, api_key, base_date):
    """단일 호출. 결과 없으면 None, 예외는 caller로 raise.

    주의: data.go.kr Open API는 'srtnCd' 키를 정확 매치 필터로 인식하지 못한다.
    'srtnCd'를 쓰면 필터가 무시되고 전체 리스트의 첫 종목이 반환되는 버그가 있다.
    따라서 'likeSrtnCd' (LIKE 매치)를 쓰고, 응답 item의 srtnCd가 요청 코드와
    일치하는지 sanity check로 추가 검증한다.
    """
    params = {
        'serviceKey': api_key,
        'numOfRows': 1,
        'pageNo': 1,
        'resultType': 'json',
        'basDt': base_date,
        'likeSrtnCd': srtn_cd,
    }
    resp = requests.get(BASE_URL, params=params, timeout=10)
    resp.raise_for_status()
    items = resp.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
    if not items:
        return None
    item = items[0] if isinstance(items, list) else items

    # Sanity check: 응답이 실제로 요청한 종목코드인지 확인.
    # 불일치 시 None 반환 → caller의 fallback 로직(yesterday 등)이 동작.
    resp_srtn_cd = str(item.get('srtnCd', '')).strip()
    if resp_srtn_cd != str(srtn_cd).strip():
        logger.warning(
            f"kr_stock srtnCd mismatch: requested={srtn_cd} got={resp_srtn_cd} "
            f"(base_date={base_date}) — API 필터 실패 가능성, 결과 폐기"
        )
        return None

    return _parse_stock_item(item)


def fetch_kr_stock(srtn_cd, api_key, base_date=None):
    """
    단일 종목 가격 fetch.
    base_date를 지정하면 그 날짜만 시도(테스트용).
    base_date=None이면 오늘 → 어제 순서로 fallback.
    """
    if base_date is not None:
        try:
            return _request_stock(srtn_cd, api_key, base_date)
        except Exception as e:
            logger.error(f"Failed to fetch KR stock {srtn_cd} on {base_date}: {e}")
            return None

    def _do(bd):
        return _request_stock(srtn_cd, api_key, bd)

    data, _ = try_fetch_with_fallback(_do, label=f"kr_stock {srtn_cd}")
    return data


def get_kr_stock_data(items):
    """
    items: list of {ticker (6-digit code), name, type} — types: STOCK_KR, ETF_KR
    Returns: {ticker: {name, type, close, prev_close, change_pct, volume, market}}
    """
    api_key = os.getenv('DATA_GO_KR_API_KEY')
    if not api_key:
        logger.error("DATA_GO_KR_API_KEY not set in .env")
        return {}

    results = {}
    for item in items:
        ticker = item['ticker']
        print(f"Fetching KR stock data for {ticker} ({item['name']})...")
        data = fetch_kr_stock(ticker, api_key)
        if data:
            results[ticker] = {
                'name': item['name'],
                'type': item['type'],
                **data,
            }
        else:
            logger.warning(f"No data for {ticker} (오늘·어제 모두 실패 — 공휴일 또는 API 장애)")

    return results
