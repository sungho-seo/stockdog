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
import math
import os
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 투심 게이지 (Phase B). An HONEST sentiment PROXY — NOT the CNN F&G index.
# score = round(mean of 3 equal-weight 0-100 sub-scores):
#   1) 수급(su_geup): market 외국인+기관 net-buy (억원, KOSPI+KOSDAQ summed),
#      tanh-normalized → 0-100. 0 net = 50 (neutral); strong inflow → ~100,
#      strong outflow → ~0. Scale picked so a "big" day (~±3조 = ±30,000억)
#      lands near the rails without saturating on ordinary days.
#   2) breadth: 상승/(상승+하락) × 100 across KOSPI+KOSDAQ combined.
#   3) tilt(등락비): 상한/(상한+하한+1) mapped to 0-100. With few 상/하한 this
#      sits near the neutral 50; a 상한-skew lifts it, 하한-skew sinks it.
# NO decimals (prior decision). Sub-scores stored for the honest breakdown.
# ---------------------------------------------------------------------------
_SUGEUP_SCALE_EOK = 15000.0   # tanh half-saturation point (억원), ~1.5조


def _clamp01_100(x):
    """Clamp to [0,100] int."""
    try:
        return int(max(0, min(100, round(x))))
    except (ValueError, TypeError):
        return None


def _sugeup_subscore(investor_flows):
    """수급 sub-score 0-100 from market 외국인+기관 net-buy (억원).

    Sums foreign+institutional across KOSPI & KOSDAQ, tanh-normalizes around 0
    → 50 (neutral), inflow → >50, outflow → <50. None when no flows.
    """
    if not isinstance(investor_flows, dict):
        return None
    market = investor_flows.get("market")
    if not isinstance(market, dict):
        return None
    net = 0.0
    seen = False
    for mk in market.values():
        if not isinstance(mk, dict):
            continue
        for key in ("foreign", "institutional"):
            v = mk.get(key)
            if isinstance(v, (int, float)):
                net += v
                seen = True
    if not seen:
        return None
    return _clamp01_100(50.0 + 50.0 * math.tanh(net / _SUGEUP_SCALE_EOK))


def _breadth_subscore(breadth):
    """breadth sub-score = 상승/(상승+하락) × 100 across KOSPI+KOSDAQ. None on miss."""
    if not isinstance(breadth, dict):
        return None
    up = down = 0
    seen = False
    for mk in breadth.values():
        if not isinstance(mk, dict):
            continue
        u, d = mk.get("up"), mk.get("down")
        if isinstance(u, (int, float)):
            up += u
            seen = True
        if isinstance(d, (int, float)):
            down += d
            seen = True
    if not seen:
        return None
    denom = up + down
    if denom <= 0:
        return 50
    return _clamp01_100(up / denom * 100.0)


def _tilt_subscore(breadth):
    """등락비 sub-score from 상한/하한 across both markets.

    limit_up/(limit_up+limit_down+1) → 0..1, mapped so balanced/none → ~50,
    상한-skew → up, 하한-skew → down. None when breadth absent.
    """
    if not isinstance(breadth, dict):
        return None
    lu = ld = 0
    seen = False
    for mk in breadth.values():
        if not isinstance(mk, dict):
            continue
        u, d = mk.get("limit_up"), mk.get("limit_down")
        if isinstance(u, (int, float)):
            lu += u
            seen = True
        if isinstance(d, (int, float)):
            ld += d
            seen = True
    if not seen:
        return None
    # Symmetric ratio centered at 50: (lu - ld)/(lu + ld + 1) ∈ (−1,1) → 0..100.
    return _clamp01_100(50.0 + 50.0 * ((lu - ld) / (lu + ld + 1.0)))


def _gauge_label(score):
    """score 0-100 → honest 5-band Korean label."""
    if score is None:
        return None
    if score >= 75:
        return "강한 매수 우위"
    if score >= 60:
        return "매수 우위"
    if score > 40:
        return "중립"
    if score > 25:
        return "매도 우위"
    return "강한 매도 우위"


def build_sentiment_gauge(investor_flows, breadth):
    """KR 투심 게이지 (Phase B). Returns:
        {"score": int 0-100, "label": str,
         "breakdown": {"su_geup": int|None, "breadth": int|None, "tilt": int|None}}
      or None when NONE of the 3 sub-scores can be computed.

    score = round(mean of the AVAILABLE sub-scores, equal weight). Storing the
    3 breakdown values keeps the gauge explainable (no black box). NEVER raises.
    """
    try:
        su = _sugeup_subscore(investor_flows)
        br = _breadth_subscore(breadth)
        ti = _tilt_subscore(breadth)
        present = [v for v in (su, br, ti) if v is not None]
        if not present:
            return None
        score = _clamp01_100(sum(present) / len(present))
        return {
            "score": score,
            "label": _gauge_label(score),
            "breakdown": {"su_geup": su, "breadth": br, "tilt": ti},
        }
    except Exception as e:
        logger.warning(f"build_sentiment_gauge failed, ignoring: {e}")
        return None


def _flows_date_matches(flow_bizdate_iso, price_base_iso):
    """True if a stock's flow bizdate is within 1 business day of its price
    base_date — so we don't stitch mismatched-date 수급 onto a price.

    Both args are ISO 'YYYY-MM-DD' (or falsy). Missing either → False
    (conservative: drop the flows rather than risk a date mismatch). Falls back
    to a calendar-day comparison if the business-day helper is unavailable.
    """
    if not flow_bizdate_iso or not price_base_iso:
        return False
    if flow_bizdate_iso == price_base_iso:
        return True
    try:
        d1 = datetime.strptime(flow_bizdate_iso, "%Y-%m-%d").date()
        d2 = datetime.strptime(price_base_iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    try:
        from utils.kr_date import business_days_between
        return business_days_between(d1, d2) <= 1
    except Exception:
        return abs((d1 - d2).days) <= 1


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
    # P2 (수급): best-effort dict from collectors.kr_investor_flow (or None).
    flows_in = (data or {}).get("investor_flows")

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

    # ---- K7 대형주 basket (P3-A) ----
    # zip prices + flows BY CODE, in config order (the order get_kr_stock_data
    # inserted them — Python dict preserves insertion order). NOT sorted by
    # change%. Tolerant → [] when unavailable. data-date discipline: a stock's
    # flow bizdate must be within 1 business day of its price base_date, else
    # flows:null (don't stitch mismatched dates).
    k7_prices = (data or {}).get("kr_k7_prices", {}) or {}
    k7_flows = (data or {}).get("kr_k7_flows", {}) or {}
    # Phase A2: per-stock 외국인 연속 streak ({code: {streak_days, direction}}).
    k7_streaks = (data or {}).get("kr_k7_foreign_streaks", {}) or {}
    k7 = []
    for code, pd in k7_prices.items():
        if not pd:
            continue
        price_base_iso = _yyyymmdd_to_iso(pd.get("base_date") or "")
        fl = k7_flows.get(code) or k7_flows.get(str(code))
        flows_out = None
        if fl:
            if _flows_date_matches(fl.get("bizdate"), price_base_iso):
                # Phase A2: ride the 외국인 연속 streak alongside the flows.
                st = k7_streaks.get(code) or k7_streaks.get(str(code))
                foreign_streak = None
                if isinstance(st, dict) and st.get("streak_days") and st.get("direction"):
                    foreign_streak = {
                        "streak_days": st.get("streak_days"),
                        "direction": st.get("direction"),
                    }
                flows_out = {
                    "individual": fl.get("individual"),
                    "foreign": fl.get("foreign"),
                    "institutional": fl.get("institutional"),
                    "unit": "주",
                    "foreign_ratio": fl.get("foreign_ratio"),
                    "foreign_streak": foreign_streak,
                }
        k7.append({
            "code": code,
            "name": pd.get("name"),
            "close": pd.get("close"),
            "prev_close": pd.get("prev_close"),
            "change_pct": pd.get("change_pct"),
            "volume": pd.get("volume"),
            "market": pd.get("market"),
            "flows": flows_out,
        })

    narrative = None
    if hero or story:
        narrative = {"hero": hero, "story": story}

    # ---- Phase A1 (등락 종목수) / A3 (지수 30일 추세) passthrough ----
    breadth_in = (data or {}).get("kr_breadth")
    breadth = breadth_in if isinstance(breadth_in, dict) else None
    index_history_in = (data or {}).get("kr_index_history")
    index_history = index_history_in if isinstance(index_history_in, dict) else None

    # ---- Phase B (투심 게이지) — computed in Python here (testable) ----
    sentiment_gauge = build_sentiment_gauge(flows_in, breadth)

    # ---- Phase C (업종 로테이션) — kr_sectors list passthrough ----
    # data['kr_sectors'] is a best-effort list (or None). Wrap it as a block
    # carrying the snapshot data_date so the emitter can label "N기준". The
    # endpoint returns no date of its own, so the snapshot's trading day is the
    # authoritative as-of (same fetch cadence as breadth/flows). Tolerant: a
    # missing/empty/non-list source degrades to None → emitter hides the card.
    sectors_in = (data or {}).get("kr_sectors")
    sectors = None
    if isinstance(sectors_in, list) and sectors_in:
        sectors = {"data_date": data_date, "items": sectors_in}

    # ---- 주요 일정 (P2/Phase D) — kr_calendar passthrough ----
    # data['kr_calendar'] is a best-effort {"data_date","events":[...]} dict from
    # collectors.kr_calendar (pure date math + baked constants, no network). Pass
    # it through as-is. Tolerant: a missing/empty/non-dict source degrades to None
    # → the emitter hides the card when there are no upcoming events.
    calendar_in = (data or {}).get("kr_calendar")
    calendar = calendar_in if isinstance(calendar_in, dict) else None

    # ---- 시간외(NXT) 급변동 (P2/Phase D) — kr_after_hours passthrough ----
    # data['kr_after_hours'] is a best-effort {"data_date","session","items":[...]}
    # dict from collectors.kr_after_hours (NXT 20:00 단일가 vs 정규장 종가). Pass it
    # through as-is. Tolerant: a missing/non-dict source degrades to None → the
    # emitter hides the card; a present block with empty items → graceful
    # "데이터 없음" empty-state.
    after_hours_in = (data or {}).get("kr_after_hours")
    after_hours = after_hours_in if isinstance(after_hours_in, dict) else None

    return {
        "updated": updated,
        "data_date": data_date,
        "indices": indices,
        "usd_krw": usd_krw,
        "movers": movers,
        "narrative": narrative,
        "report_slug": report_slug,
        # Phase A1 (등락 종목수): {KOSPI:{up,flat,down,limit_up,limit_down}, ...}
        # or None. The emitter renders the breadth card only when present.
        "breadth": breadth,
        # Phase A3 (지수 30일 추세): {KOSPI:{closes:[...],count}, ...} or None.
        "index_history": index_history,
        # Phase B (투심 게이지): {score,label,breakdown:{su_geup,breadth,tilt}}
        # or None. HONEST proxy (수급+breadth+등락비), NOT CNN F&G.
        "sentiment_gauge": sentiment_gauge,
        # Phase C (업종 로테이션): {data_date, items:[{name, change_pct(signed),
        # advancing, declining, steady, members}, ...]} or None. The emitter
        # renders the 섹터 등락 card only when present & non-empty.
        "sectors": sectors,
        # 주요 일정 (P2/Phase D): {data_date, events:[{name,date,type,days_until}]}
        # or None. FUTURE-only 금통위/네마녀/MSCI events; the emitter recomputes
        # D-N at build time and renders the card only when events is non-empty.
        "calendar": calendar,
        # 시간외(NXT) 급변동 (P2/Phase D): {data_date, session, items:[{name,code,
        # reg_close, nxt_price, change_pct(NXT vs 정규 종가, signed), volume}]} or
        # None. The emitter renders the 시간외 card when present (empty items → a
        # graceful empty-state; absent → hidden).
        "after_hours": after_hours,
        # P3-A (K7 대형주): per-stock price + 수급 direction (주). [] when
        # unavailable; the emitter renders the block only when non-empty.
        "k7": k7,
        # P2 (수급): the collected market-level investor flows dict
        # ({data_date, unit, market:{KOSPI,KOSDAQ}}) or None when unavailable.
        # The emitter renders the 투자자별 수급 hero only when this is present.
        "investor_flows": flows_in if isinstance(flows_in, dict) else None,
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
