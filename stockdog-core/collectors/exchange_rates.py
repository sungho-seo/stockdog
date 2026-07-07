import logging
import yfinance as yf

from utils.prior_close import prior_from_history

logger = logging.getLogger(__name__)


def get_exchange_rates():
    """
    Fetches USD/KRW exchange rate via yfinance.
    Returns: {'USD_KRW': {rate, prev_rate, change_pct}}
    """
    results = {}
    print("Fetching USD/KRW exchange rate...")
    try:
        hist = yf.Ticker('KRW=X').history(period='5d')
        if hist.empty or len(hist) < 1:
            logger.warning("No exchange rate data returned")
            return results

        rate = round(float(hist['Close'].iloc[-1]), 2)
        prior = prior_from_history(hist)
        if prior.value is not None and prior.within_window and prior.value != 0:
            prev_rate  = round(prior.value, 2)
            change_pct = round((rate - prev_rate) / prev_rate * 100, 2)
        else:
            prev_rate, change_pct = rate, None

        results['USD_KRW'] = {
            'rate': rate,
            'prev_rate': prev_rate,
            'change_pct': change_pct,
        }
    except Exception as e:
        logger.error(f"Failed to fetch USD/KRW: {e}")

    return results
