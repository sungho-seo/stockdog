import requests
from bs4 import BeautifulSoup
import logging

logger = logging.getLogger(__name__)

def fetch_institutional_holdings(ticker):
    """
    Fetches the institutional holdings summary for a given ticker.
    As a free alternative to parsing raw SEC 13F XMLs, we scrape Yahoo Finance's Holders page
    which aggregates 13F data.
    """
    url = f"https://finance.yahoo.com/quote/{ticker}/holders"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Note: Yahoo Finance DOM changes frequently. This is a basic attempt to find the major holders summary.
            # In a production environment, you might want to use a reliable API like Financial Modeling Prep or SEC API.
            holdings_data = {
                "ticker": ticker,
                "institutional_ownership_percent": None,
                "top_institutions": []
            }
            
            # Find the major holders table (approximate logic)
            tables = soup.find_all("table")
            if tables:
                # Top institutions are usually in the second table
                if len(tables) > 1:
                    inst_table = tables[1]
                    rows = inst_table.find_all("tr")
                    for row in rows[1:6]: # Get top 5
                        cols = row.find_all("td")
                        if len(cols) >= 4:
                            holder_name = cols[0].text.strip()
                            shares = cols[1].text.strip()
                            holdings_data["top_institutions"].append({
                                "name": holder_name,
                                "shares": shares
                            })
                            
            return holdings_data
    except Exception as e:
        logger.error(f"Failed to fetch 13F/Holders for {ticker}: {e}")
        
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
