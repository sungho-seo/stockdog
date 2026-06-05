"""IMPR-062: watchlist price/volume raw store — thin wrapper over m7_store.

Reuses the category-generic per-symbol store (utils/m7_store.py) with
CATEGORY="price". Captures the in-hand us_market dict from the US pipeline
(no re-fetch) and stages a vault-readable snapshot for the host-side renderer
(render_watchlist_tracker.py), which cannot read raw history scattered across
per-ticker dirs efficiently.

Storage (raw_base_dir = /notes/raw/stockdog/watchlist):
  watchlist/price/{date}.json              — per-day dump (audit trail)
  watchlist/{ticker}/price_history.json    — per-symbol time series (date desc, 180-day cap)
  watchlist/{ticker}/price_latest.json     — per-symbol latest single record
  watchlist/watchlist_snapshot.json        — staged snapshot for the renderer

C1: history_cap_days=180 EVERYWHERE (m7_store default 90 would clip the 6mo
~125pt backfill). C3: records key on trade_date (NYSE close), fall back to the
folder date only if trade_date is None — so backfill + live capture share one
date axis and dedupe works.
"""
import logging
import os

from utils import m7_store
from utils.vault_reader import read_watchlist_items

logger = logging.getLogger(__name__)

CATEGORY = "price"
HISTORY_CAP_DAYS = 180

# watchlist.md lives at /notes/_system/watchlist.md; the raw store lives under the
# watchlist raw dir, so the source markdown is two levels up + _system.
WATCHLIST_FILE_DEFAULT = "/notes/_system/watchlist.md"


def save_watchlist_day(wl_dir, fallback_date, us_market):
    """Persist one day of price/volume for every ticker in us_market.

    us_market: {ticker: {name, type, close, prev_close, change_pct, volume,
                         trade_date}}. Never raises on a single bad ticker —
    the caller wraps the whole step, but we guard per ticker too.
    """
    us_market = us_market or {}
    by_ticker = {}
    for tk, d in us_market.items():
        if not isinstance(d, dict):
            continue
        rec = {
            "date": d.get("trade_date") or fallback_date,
            "close": d.get("close"),
            "prev_close": d.get("prev_close"),
            "change_pct": d.get("change_pct"),
            "volume": d.get("volume"),
            "name": d.get("name"),
            "type": d.get("type"),
        }
        try:
            m7_store.append_per_symbol(
                wl_dir, CATEGORY, tk, rec, history_cap_days=HISTORY_CAP_DAYS
            )
            m7_store.write_per_symbol_latest(wl_dir, CATEGORY, tk, rec)
            by_ticker[tk] = rec
        except Exception as e:
            logger.warning(f"watchlist_store: skip {tk}: {e}")

    m7_store.write_per_day_dump(
        wl_dir, CATEGORY, fallback_date,
        {"category": CATEGORY, "by_ticker": by_ticker},
    )


def stage_watchlist_snapshot(wl_dir):
    """Build wl_dir/watchlist_snapshot.json for the host-side renderer.

    Shape mirrors stage_macro_snapshot's contract:
      {updated, order:"oldest->newest",
       tickers:{TK:{name, type,
                    history:[{date,close,volume,change_pct} oldest->newest ≤180],
                    latest:{...}}}}

    Iterates tickers from read_watchlist_items (stable file order); skips any
    ticker without history. Atomic write, ensure_ascii=False. Never raises.
    """
    try:
        wl_file = os.getenv("WATCHLIST_FILE", WATCHLIST_FILE_DEFAULT)
        items = read_watchlist_items(wl_file, types=("STOCK", "ETF", "INDEX_US"))

        tickers = {}
        for item in items:
            tk = item["ticker"]
            hist_desc = m7_store.read_per_symbol_history(wl_dir, CATEGORY, tk)
            if not hist_desc:
                continue  # no data yet for this ticker — skip
            # read_per_symbol_history returns date-DESC; reverse to oldest->newest.
            hist_oldest = list(reversed(hist_desc))[-HISTORY_CAP_DAYS:]
            history = [
                {
                    "date": r.get("date"),
                    "close": r.get("close"),
                    "volume": r.get("volume"),
                    "change_pct": r.get("change_pct"),
                }
                for r in hist_oldest
            ]
            latest = m7_store.read_per_symbol_latest(wl_dir, CATEGORY, tk)
            tickers[tk] = {
                "name": item.get("name"),
                "type": item.get("type"),
                "history": history,
                "latest": latest or {},
            }

        from datetime import date as _date
        payload = {
            "updated": _date.today().isoformat(),
            "order": "oldest->newest",
            "tickers": tickers,
        }
        path = os.path.join(wl_dir, "watchlist_snapshot.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        logger.info(f"Watchlist snapshot staged: {path} [{len(tickers)} tickers]")
        return path
    except Exception as e:
        logger.warning(f"stage_watchlist_snapshot failed, ignoring: {e}")
        return None
