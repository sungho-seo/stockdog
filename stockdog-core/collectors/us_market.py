import logging
import yfinance as yf

from utils.prior_close import prior_from_history

logger = logging.getLogger(__name__)


def get_us_market_data(items):
    """
    Fetches price data for US stocks, ETFs, and indices via yfinance.
    items: list of {ticker, name, type} — types: STOCK, ETF, INDEX_US
    Returns: {ticker: {name, type, close, prev_close, change_pct, volume,
                       trade_date: 'YYYY-MM-DD' | None}}
    trade_date: yfinance 응답의 마지막 close 시점 (실제 거래일).

    Notes on splits: auto_adjust=True ensures close/open/high/low are back-adjusted
    for splits/dividends so that close and prev_close are on the same basis.
    A plausibility guard detects suspicious large changes (>40%) with clean split
    ratios (2:1, 3:1, etc.) and nulls change_pct to prevent phantom moves.
    """
    results = {}
    for item in items:
        ticker = item['ticker']
        print(f"Fetching US market data for {ticker}...")
        try:
            ticker_obj = yf.Ticker(ticker)
            # auto_adjust=True ensures close/open/high/low are back-adjusted for splits/dividends
            hist = ticker_obj.history(period='5d', auto_adjust=True)
            if hist.empty or len(hist) < 1:
                logger.warning(f"No data returned for {ticker}")
                continue

            close = round(float(hist['Close'].iloc[-1]), 2)
            prior = prior_from_history(hist)
            if prior.value is not None and prior.within_window:
                prev_close = round(prior.value, 2)
            else:
                prev_close = close
            volume = int(hist['Volume'].iloc[-1]) if 'Volume' in hist.columns else 0
            # yfinance Timestamp → 'YYYY-MM-DD'. Index가 tz-aware일 수도 있어 strftime만 사용.
            try:
                trade_date = hist.index[-1].strftime('%Y-%m-%d')
            except Exception:
                trade_date = None

            # Compute change_pct with plausibility guard for split detection.
            change_pct = None
            if prior.value is not None and prior.within_window and prev_close != 0:
                change_pct = round((close - prev_close) / prev_close * 100, 2)

                # Plausibility guard: if |change_pct| > 40% and ratio looks like a clean split,
                # null it (indicates unhandled or data-lag split scenario).
                if abs(change_pct) > 40:
                    ratio = close / prev_close
                    # Common splits: 2:1, 3:1, 4:1 (forward) or 1:2, 1:3, 1:4 (reverse).
                    common_ratios = [0.25, 0.333, 0.5, 2.0, 3.0, 4.0]
                    if any(abs(ratio - r) < 0.05 for r in common_ratios):
                        logger.warning(
                            f"{ticker}: suspicious change_pct={change_pct}% with ratio={ratio:.2f} "
                            f"(detected split-like pattern); setting change_pct=None"
                        )
                        change_pct = None

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
