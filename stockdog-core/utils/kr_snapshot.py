"""KR market snapshot dump (P1 of the 국장/KR public garden page).

Writes ~/service/skyler/raw/stockdog/kr/kr_snapshot.json from the already-
collected KR pipeline dicts (kr_indices / kr_stocks / exchange) — NO new
collector, NO extra external API call. Mirrors the macro snapshot pattern
(utils/metrics_history.py::stage_macro_snapshot): atomic write (tmp + os.replace),
mkdir -p, ensure_ascii=False, NEVER raises (caller is wrapped too).

Shape (P1):
{
  "updated": "2026-06-12",        # KST today (발간일)
  "data_date": "2026-06-11",      # KR trading day the data is as-of
  "indices": {
    "KOSPI": {close, prev_close, change_pct, volume, base_date},
    "KOSDAQ": {...}
  },
  "usd_krw": {"rate": 1520.98, "change_pct": -0.27},
  "movers": [
    {"name":"NAVER","code":"035420","close":224000,"prev_close":227000,
     "change_pct":-1.32,"volume":2218764,"market":"KOSPI"}, ...
  ],
  "narrative": {"hero": "...", "story": "..."},   # one-liner + 2-3문단
  "report_slug": "/daily-reports/2026-06-12-kr",
  "investor_flows": null          # reserved for P2 (수급)
}

investor_flows is ALWAYS null in P1 — the emitter renders the 수급 hero only
when present, so it is absent on the P1 page.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def _yyyymmdd_to_iso(s):
    """'20260611' → '2026-06-11'. 형식 이상 시 원본 반환."""
    if not s or len(s) != 8 or not s.isdigit():
        return s
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


# KOSPI/KOSDAQ index codes used as the snapshot index keys.
_INDEX_KEYS = ("KOSPI", "KOSDAQ")


def build_kr_snapshot(data, *, updated, data_date=None,
                      report_slug=None, hero=None, story=None):
    """Build the kr_snapshot dict from the KR pipeline `data` dict.

    data: {'kr_indices': {...}, 'kr_stocks': {...}, 'exchange': {...}}
      (the exact shape KRPipeline.collect() returns).
    updated: 발간일 ISO (YYYY-MM-DD).
    data_date: 거래일 ISO; when None, derived from KOSPI/KOSDAQ base_date.
    report_slug / hero / story: optional narrative + report link.

    Tolerant: every field degrades to None/[] on missing data; never raises.
    """
    indices_in = (data or {}).get("kr_indices", {}) or {}
    stocks_in = (data or {}).get("kr_stocks", {}) or {}
    exchange_in = (data or {}).get("exchange", {}) or {}

    # ---- indices (KOSPI / KOSDAQ) ----
    indices = {}
    for key in _INDEX_KEYS:
        d = indices_in.get(key)
        if not d:
            continue
        indices[key] = {
            "close": d.get("close"),
            "prev_close": d.get("prev_close"),
            "change_pct": d.get("change_pct"),
            "volume": d.get("volume"),
            "base_date": _yyyymmdd_to_iso(d.get("base_date") or ""),
        }

    # ---- derive data_date from KOSPI/KOSDAQ base_date when not given ----
    if not data_date:
        for key in _INDEX_KEYS:
            bd = (indices.get(key) or {}).get("base_date")
            if bd:
                data_date = bd
                break

    # ---- usd_krw ----
    usd = exchange_in.get("USD_KRW", {}) or {}
    usd_krw = None
    if usd.get("rate") is not None:
        usd_krw = {"rate": usd.get("rate"), "change_pct": usd.get("change_pct")}

    # ---- movers (individual stocks) ----
    movers = []
    for code, d in stocks_in.items():
        if not d:
            continue
        movers.append({
            "name": d.get("name"),
            "code": code,
            "close": d.get("close"),
            "prev_close": d.get("prev_close"),
            "change_pct": d.get("change_pct"),
            "volume": d.get("volume"),
            "market": d.get("market"),
        })
    # Sort by |change_pct| desc so the biggest movers lead (null-safe).
    movers.sort(key=lambda m: abs(m.get("change_pct") or 0), reverse=True)

    narrative = None
    if hero or story:
        narrative = {"hero": hero, "story": story}

    return {
        "updated": updated,
        "data_date": data_date,
        "indices": indices,
        "usd_krw": usd_krw,
        "movers": movers,
        "narrative": narrative,
        "report_slug": report_slug,
        "investor_flows": None,   # P2 (수급) — null in P1 → emitter hides hero
    }


def write_kr_snapshot(snapshot_path, snapshot):
    """Atomic write of the snapshot dict (tmp + os.replace, mkdir -p).

    Mirrors stage_macro_snapshot's write tail. Never raises — logs + returns
    None on failure so the caller (pipeline) is never aborted.
    """
    try:
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        tmp = snapshot_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp, snapshot_path)
        logger.info(f"wrote kr_snapshot → {snapshot_path}")
        return snapshot_path
    except Exception as e:
        logger.warning(f"write_kr_snapshot failed, ignoring: {e}")
        return None
