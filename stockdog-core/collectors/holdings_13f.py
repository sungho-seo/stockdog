import yfinance as yf
import logging
import pandas as pd

logger = logging.getLogger(__name__)

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
    """
    Given a list of tickers, returns their institutional holdings.
    """
    results = {}
    for ticker in tickers:
        print(f"Fetching 13F Institutional data for {ticker}...")
        results[ticker] = fetch_institutional_holdings(ticker)
    return results

if __name__ == "__main__":
    import json
    # Test execution
    print(json.dumps(get_all_13f_data(["TSLA", "ANET"]), indent=2))
