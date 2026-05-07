import requests
from bs4 import BeautifulSoup
import json
import logging

logger = logging.getLogger(__name__)

def fetch_fear_and_greed():
    """
    Fetches the current CNN Fear & Greed index using a public CNN API endpoint or scraping.
    """
    try:
        # CNN uses a specific API for the index now
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            score = data.get('fear_and_greed', {}).get('score')
            rating = data.get('fear_and_greed', {}).get('rating')
            return {"score": round(score), "rating": rating}
    except Exception as e:
        logger.error(f"Failed to fetch Fear & Greed: {e}")
    
    return {"score": None, "rating": "Unknown"}

def fetch_yahoo_finance_quote(ticker):
    """
    Fetches a basic quote from Yahoo Finance for indices like VIX or TNX (10Y Yield).
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            result = data['chart']['result'][0]
            current_price = result['meta']['regularMarketPrice']
            previous_close = result['meta']['chartPreviousClose']
            change_percent = ((current_price - previous_close) / previous_close) * 100
            
            return {
                "price": current_price,
                "change_percent": round(change_percent, 2)
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
