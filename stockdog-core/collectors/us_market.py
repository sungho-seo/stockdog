import logging
import yfinance as yf

logger = logging.getLogger(__name__)


def get_us_market_data(items):
    """
    Fetches price data for US stocks, ETFs, and indices via yfinance.
    items: list of {ticker, name, type} — types: STOCK, ETF, INDEX_US
    Returns: {ticker: {name, type, close, prev_close, change_pct, volume,
                       trade_date: 'YYYY-MM-DD' | None}}
    trade_date: yfinance 응답의 마지막 close 시점 (실제 거래일).
    """
    results = {}
    for item in items:
        ticker = item['ticker']
        print(f"Fetching US market data for {ticker}...")
        try:
            hist = yf.Ticker(ticker).history(period='5d')
            if hist.empty or len(hist) < 1:
                logger.warning(f"No data returned for {ticker}")
                continue

            close = round(float(hist['Close'].iloc[-1]), 2)
            prev_close = round(float(hist['Close'].iloc[-2]), 2) if len(hist) > 1 else close
            change_pct = round((close - prev_close) / prev_close * 100, 2)
            volume = int(hist['Volume'].iloc[-1]) if 'Volume' in hist.columns else 0
            # yfinance Timestamp → 'YYYY-MM-DD'. Index가 tz-aware일 수도 있어 strftime만 사용.
            try:
                trade_date = hist.index[-1].strftime('%Y-%m-%d')
            except Exception:
                trade_date = None

            results[ticker] = {
                'name': item['name'],
                'type': item['type'],
                'close': close,
                'prev_close': prev_close,
                'change_pct': change_pct,
                'volume': volume,
                'trade_date': trade_date,
            }
        except Exception as e:
            logger.error(f"Failed to fetch {ticker}: {e}")

    return results
