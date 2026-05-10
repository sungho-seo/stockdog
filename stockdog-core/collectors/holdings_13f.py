import os
import json
import yfinance as yf
import logging
import pandas as pd
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "cache", "13f_cache.json")
CACHE_TTL_DAYS = 30


def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        fetched_at = datetime.fromisoformat(cache["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < timedelta(days=CACHE_TTL_DAYS):
            return cache["data"]
    except (FileNotFoundError, KeyError, ValueError):
        pass
    return None


def _save_cache(data):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}, f, indent=2)

def fetch_institutional_holdings(ticker):
    """
    Fetches the institutional holdings summary for a given ticker using yfinance.
    This bypasses Yahoo's anti-bot measures to get reliable 13F-style data.
    """
    try:
        holdings_data = {
            "ticker": ticker,
            "institutional_ownership_percent": None,
            "top_institutions": []
        }
        
        t = yf.Ticker(ticker)
        df = t.institutional_holders
        
        if df is not None and not df.empty:
            # Grab the top 5 institutions
            top_5 = df.head(5)
            
            for index, row in top_5.iterrows():
                holder_name = row.get("Holder", "Unknown")
                shares = row.get("Shares", 0)
                
                # Format shares with commas
                if pd.isna(shares):
                    shares_str = "Unknown"
                elif isinstance(shares, (int, float)):
                    shares_str = f"{int(shares):,}"
                else:
                    shares_str = str(shares)
                    
                holdings_data["top_institutions"].append({
                    "name": holder_name,
                    "shares": shares_str
                })
                
        return holdings_data
    except Exception as e:
        logger.error(f"Failed to fetch 13F/Holders for {ticker} via yfinance: {e}")
        
    return {"ticker": ticker, "error": "Could not fetch data"}

def get_all_13f_data(tickers):
    cached = _load_cache()
    if cached is not None:
        print("13F cache is fresh (< 30 days). Skipping yfinance fetch.")
        return cached

    print("13F cache expired or missing. Fetching from yfinance...")
    results = {}
    for ticker in tickers:
        print(f"Fetching 13F Institutional data for {ticker}...")
        results[ticker] = fetch_institutional_holdings(ticker)
    _save_cache(results)
    return results

if __name__ == "__main__":
    import json
    # Test execution
    print(json.dumps(get_all_13f_data(["TSLA", "ANET"]), indent=2))
