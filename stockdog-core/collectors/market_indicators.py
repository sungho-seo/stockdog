import requests
from bs4 import BeautifulSoup
import json
import logging
from datetime import datetime

from utils.prior_close import select_prior

logger = logging.getLogger(__name__)

def fetch_fear_and_greed_full():
    """Returns raw CNN graphdata JSON dict, or None on failure."""
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "fear_and_greed" in data:
                return data
    except Exception as e:
        logger.error(f"Failed to fetch F&G full: {e}")
    return None

def fetch_fear_and_greed():
    """Compat wrapper — returns {score, rating} only."""
    data = fetch_fear_and_greed_full()
    if data:
        fg = data.get("fear_and_greed", {})
        score = fg.get("score")
        rating = fg.get("rating")
        if score is not None:
            return {"score": round(score), "rating": rating}
    return {"score": None, "rating": "Unknown"}

def fetch_yahoo_finance_quote(ticker):
    """
    Fetches a basic quote from Yahoo Finance for indices like VIX or TNX (10Y Yield).
    Uses 5d range to get reliable previous close from historical closes array,
    avoiding stale chartPreviousClose for indices like ^VIX.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            result = data['chart']['result'][0]
            current_price = result['meta']['regularMarketPrice']

            # Extract previous close from historical closes array using prior_close helper.
            # Build (date, value) pairs from timestamps and closes.
            timestamps = result.get('timestamp', []) or []
            closes = result.get('indicators', {}).get('quote', [{}])[0].get('close', []) or []

            pairs = []
            for ts, close_val in zip(timestamps, closes):
                try:
                    date_obj = datetime.utcfromtimestamp(ts).date()
                    pairs.append((date_obj, close_val))
                except (TypeError, ValueError):
                    pass

            # Use select_prior to get the prior close (n=1, respecting gap window)
            previous_close = None
            if pairs:
                prior_result = select_prior(pairs, pairs[-1][0], n=1)
                if prior_result.value is not None and prior_result.within_window:
                    previous_close = prior_result.value

            # Fallback to chartPreviousClose if prior extraction failed
            if previous_close is None:
                previous_close = result['meta'].get('chartPreviousClose')

            # Calculate change percent if we have both prices
            change_percent = None
            if current_price is not None and previous_close is not None and previous_close != 0:
                change_percent = ((current_price - previous_close) / previous_close) * 100
                change_percent = round(change_percent, 2)

            return {
                "price": current_price,
                "change_percent": change_percent
            }
    except Exception as e:
        logger.error(f"Failed to fetch Yahoo Finance for {ticker}: {e}")

    return {"price": None, "change_percent": None}

def get_all_indicators():
    """
    Aggregates all key market indicators.
    """
    print("Fetching Fear & Greed Index...")
    fgi = fetch_fear_and_greed()
    
    print("Fetching VIX...")
    vix = fetch_yahoo_finance_quote("^VIX")
    
    print("Fetching US 10-Year Treasury Yield...")
    tnx = fetch_yahoo_finance_quote("^TNX")
    
    return {
        "fear_and_greed": fgi,
        "vix": vix,
        "us_10y_yield": tnx
    }

if __name__ == "__main__":
    # Test execution
    print(json.dumps(get_all_indicators(), indent=2))
