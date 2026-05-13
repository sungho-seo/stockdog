"""
한국장 개장 전(08:00 KST) preopen 분석용 야간/아시아 데이터 수집.

수집 대상 (yfinance 한정):
- NKD=F  : Nikkei 225 선물 (CME)
- HSI=F  : Hang Seng 선물
- EWY    : MSCI Korea ETF (ADR proxy)
- CPNG   : 쿠팡 ADR
- KB     : KB금융 ADR

NOTE:
- KOSPI200 야간선물(KRX 야간) / USD/KRW NDF는 yfinance가 안정적으로 제공하지 않아 skip.
  Bloomberg/금융위 API 도입 시 별도 collector로 확장 예정.
- 빈 결과 시 예외를 raise하지 않고 빈 dict + 경고 로그만 — main 파이프라인 graceful degrade 유지.
"""
import logging
from datetime import datetime, timezone

import yfinance as yf

logger = logging.getLogger(__name__)

# (ticker, 표시명, 통화) — 추가 watchlist는 추후 vault 읽기로 확장 가능
ASIA_OVERNIGHT_TICKERS = [
    ("NKD=F", "Nikkei 225 Futures", "USD"),
    # HSI=F: yfinance 404 (delisted symbol) — TODO: ^HSI 현물 또는 다른 데이터원 검토.
    # 매일 ERROR 로그가 찍혀 모니터링 false positive 유발 → 임시 비활성.
    # ("HSI=F", "Hang Seng Futures",  "HKD"),
    ("EWY",   "MSCI Korea ETF",     "USD"),
    ("CPNG",  "Coupang ADR",        "USD"),
    ("KB",    "KB Financial ADR",   "USD"),
]


def _fetch_one(ticker: str, name: str, currency: str) -> dict | None:
    """단일 ticker fetch. 실패 시 None."""
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty or len(hist) < 1:
            logger.warning(f"[asia_overnight] no data for {ticker}")
            return None

        last_close = round(float(hist["Close"].iloc[-1]), 2)
        prev_close = round(float(hist["Close"].iloc[-2]), 2) if len(hist) > 1 else last_close
        change_abs = round(last_close - prev_close, 2)
        change_pct = round((last_close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

        return {
            "name": name,
            "last_close": last_close,
            "prev_close": prev_close,
            "change_abs": change_abs,
            "change_pct": change_pct,
            "currency": currency,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    except Exception as e:
        logger.error(f"[asia_overnight] failed to fetch {ticker}: {e}")
        return None


def collect_asia_overnight() -> dict:
    """
    Returns:
        dict — {ticker: {name, last_close, prev_close, change_abs, change_pct, currency, fetched_at}}
        실패한 ticker는 dict에서 제외. 전부 실패 시 빈 dict 반환 (raise 안 함).
    """
    results: dict = {}
    for ticker, name, currency in ASIA_OVERNIGHT_TICKERS:
        print(f"Fetching asia overnight {ticker} ({name})...")
        data = _fetch_one(ticker, name, currency)
        if data:
            results[ticker] = data

    if not results:
        logger.warning("[asia_overnight] all tickers failed — returning empty dict")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    import json
    out = collect_asia_overnight()
    print("\n=== asia_overnight result ===")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\ntotal tickers: {len(out)} / {len(ASIA_OVERNIGHT_TICKERS)}")
