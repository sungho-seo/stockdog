#!/usr/bin/env python3
"""Render the signals aggregation public markdown tracker (IMPR-063).

Host-side, stdlib only (json, sys, statistics, datetime, pathlib). This page is a
READ-ONLY RE-AGGREGATION of the SAME staged snapshots the other trackers consume:
    raw/stockdog/m7/{short,insider}/<date>.json + m7/<TK>/{short,insider}_history.json
    raw/stockdog/m7/metrics_snapshot.json   (F&G / VIX / 10Y)
    raw/stockdog/macro/macro_snapshot.json
    raw/stockdog/watchlist/watchlist_snapshot.json
It writes a single public page:
    <vault_root>/10_Public/trackers/signals.md

It surfaces, in one place, ONLY the threshold-exceeding observations across the
four domain trackers (M7 short, M7 insider, macro, watchlist) plus F&G, scores
them, and bundles obvious cross-signals. It NEVER emits 매수/매도 calls — every
line is an observation.
"""

# ===========================================================================
# thresholds/logic copied from render_{m7,macro,watchlist}_tracker.py
#   — keep in sync; signals is read-only on same snapshots.
# Helpers/constants below are duplicated BYTEWISE (esp. compute_insider_summary
# and its helpers) so the three source renderers stay byte-for-byte unchanged.
# If a threshold/logic is fixed there, mirror it here and vice-versa — do NOT
# import from them. raw/ is READ-ONLY: this script only reads and writes under
# 10_Public/.
# ===========================================================================

import json
import os
import re
import sys
import tempfile
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from statistics import mean

# ---------------------------------------------------------------------------
# COPIED constants — M7 (render_m7_tracker.py)
# ---------------------------------------------------------------------------
TICKERS = ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]

NETFLOW_EXCLUDE = {"TaxWithholding", "Gift", "Grant", "Exercise"}
NETFLOW_WINDOW_DAYS = 14   # trailing calendar-day window for net $ flow
CLUSTER_WINDOW_DAYS = 5    # ≥3 distinct insiders same dir within ~5d → cluster
SHORT_SPARK_DAYS = 13      # cap of short-ratio points used for sparkline/mean

SPARK_CHARS = "▁▂▃▄▅▆▇█"

# ---------------------------------------------------------------------------
# COPIED constants — macro (render_macro_tracker.py)
# ---------------------------------------------------------------------------
FOMC_DATES = [
    "2026-01-28", "2026-03-18", "2026-05-06",
    "2026-06-17", "2026-07-29", "2026-09-16",
    "2026-10-28", "2026-12-16",
    # 2027: TODO confirm from federalreserve.gov
]

# ---------------------------------------------------------------------------
# COPIED constants — watchlist (render_watchlist_tracker.py)
# ---------------------------------------------------------------------------
_WL_CATEGORIES = [
    ("지수 ETF", ["SPY", "QQQ"]),
    ("개별 종목", ["TSLA", "ANET", "IONQ"]),
    ("레버리지/인버스", ["ETHU", "METU", "NVDL", "TSLL", "TSLT", "ANEL", "FNGU", "BULZ"]),
]
LEVERAGED = set(_WL_CATEGORIES[2][1])

BUILDING_THRESHOLD = 30   # < this many points → "(~Nd, building)" honesty label
VOL_WINDOW = 20           # trailing window for volume-vs-average
VOL_SPIKE_MULT = 2.0      # volume ratio ≥ this → 거래량▲ flag
PROX = 0.02               # within 2% of window high/low → 근접 flag
D5_LOOKBACK = 5           # Δ5d lookback (closes[-1] vs closes[-1-5])

# ---------------------------------------------------------------------------
# IMPR-063 thresholds (signals-specific — initial estimates, recalibrate @30d)
# ---------------------------------------------------------------------------
SHORT_DEV_PP = 4.0        # |공매도 비중 − history mean| ≥ this (%p) → flag
SHORT_DEV_EXCESS1 = 6.0   # +1 threshold_excess at/above this
SHORT_DEV_EXCESS2 = 8.0   # +2 threshold_excess at/above this
SHORT_BREADTH = 5         # ≥ this many of 7 above-mean → breadth context line

INSIDER_NETFLOW_USD = 2_000_000     # |net flow| ≥ → flag
INSIDER_NETFLOW_EXCESS1 = 5_000_000
INSIDER_NETFLOW_EXCESS2 = 10_000_000
INSIDER_CLUSTER = 3       # ≥ this distinct insiders → cluster flag (already in summary)

YIELD_BPS = 15.0          # |Δ5d| of 10Y/2Y ≥ this (bps) → flag
SPREAD_BPS = 10.0         # |Δ5d| of 10Y-2Y spread ≥ this (bps) → flag
FOMC_BLACKOUT_PRE = 10    # blackout onset window [meeting−10d, meeting+1d]
FOMC_BLACKOUT_POST = 1

VOL_EXCESS1 = 3.0         # vol ratio +1 excess
VOL_EXCESS2 = 4.0         # vol ratio +2 excess
WL_D5_PCT = 8.0           # |Δ5d %| ≥ this → tierC flag

FG_JUMP = 8.0             # |Δ1d| of F&G ≥ this → tierC flag
FG_JUMP_EXCESS1 = 12.0
FG_ZONE_BOUNDS = (24, 44, 55, 74)  # regime zone boundaries

# scoring weights
TIER_BASE = {"A": 3, "B": 2, "C": 1}
EVENT_BONUS = 2           # state_changed
RARITY_BONUS = 2
LEVERAGE_PENALTY = 2
CS_BONUS = 3              # cross-signal card = max(leg scores) + this

SCORE_MAJOR = 6           # 🔴 주요
SCORE_WATCH = 3           # 🟡 관찰 (3..5)
WATCH_CAP = 8             # 관찰 list cap

# cross-signal CS-3: macro yield-Δ AND a rate-sensitive name moves same run
RATE_SENSITIVE = {"TSLA", "NVDA", "IONQ", "ANET", "TSLL", "TSLT",
                  "NVDL", "ETHU", "BULZ", "FNGU", "METU", "ANEL"}


# ===========================================================================
# COPIED helpers — bytewise from render_m7_tracker.py
# ===========================================================================
def load_json(path: Path):
    """Read a JSON file; None on absence / parse error."""
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_category(vault_root: Path, category: str, want_date: str):
    """Load <vault_root>/raw/stockdog/m7/<category>/<want_date>.json (newest fallback)."""
    base = vault_root / "raw" / "stockdog" / "m7" / category
    target = base / f"{want_date}.json"
    if target.is_file():
        chosen = target
    else:
        candidates = sorted(base.glob("2*.json"))
        if not candidates:
            return None
        chosen = candidates[-1]
    try:
        with chosen.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_short_history(vault_root: Path, ticker: str) -> list:
    data = load_json(vault_root / "raw" / "stockdog" / "m7" / ticker / "short_history.json")
    return data if isinstance(data, list) else []


def load_insider_history(vault_root: Path, ticker: str) -> list:
    data = load_json(vault_root / "raw" / "stockdog" / "m7" / ticker / "insider_history.json")
    return data if isinstance(data, list) else []


def load_metrics_snapshot(vault_root: Path):
    return load_json(vault_root / "raw" / "stockdog" / "m7" / "metrics_snapshot.json")


def fg_zone(score) -> str:
    """CNN-style Fear & Greed zone label."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "—"
    if s <= 24:
        return "Extreme Fear"
    if s <= 44:
        return "Fear"
    if s <= 55:
        return "Neutral"
    if s <= 74:
        return "Greed"
    return "Extreme Greed"


def drift_flag(values_oldest_to_newest) -> str:
    """'3d↑'/'3d↓'/'—' — monotone run of the last 3 points. ≥3 only."""
    nums = [float(v) for v in values_oldest_to_newest if v is not None]
    if len(nums) < 3:
        return "—"
    a, b, c = nums[-3], nums[-2], nums[-1]
    if c > b > a:
        return "3d↑"
    if c < b < a:
        return "3d↓"
    return "—"


def role_is_senior(role: str) -> bool:
    """CEO / CFO / Chair / President — higher-signal roles."""
    if not role:
        return False
    r = role.lower()
    return any(k in r for k in (
        "chief executive", "ceo",
        "chief financial", "cfo",
        "chair", "president",
    ))


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _dedupe_insider_txns(history: list) -> list:
    """insider_history.json holds per-day snapshots that REPEAT the same txn
    across days. Dedupe to distinct transactions by a stable composite key.
    Returns a flat list of transaction dicts.
    """
    seen = {}
    for day in history:
        for t in day.get("transactions") or []:
            key = (
                t.get("accession"),
                t.get("date"),
                t.get("insider_name"),
                t.get("action"),
                t.get("shares"),
                t.get("value_usd"),
            )
            seen[key] = t
    return list(seen.values())


def compute_insider_summary(history: list, asof: _date):
    """Per-ticker insider summary over the trailing window.

    Returns dict with:
      net_flow      — Σ Buy value − Σ Sell value (open-market only, exclusions dropped)
      buy_usd / sell_usd
      window_txns   — distinct txns whose transaction date is within window
      breaches      — list of breached txns in window
      senior_breaches — breached txns by CEO/CFO/Chair in window
      cluster       — int N if ≥3 distinct insiders same direction within ~5d, else 0
      cluster_dir   — 'Sell'/'Buy' if cluster
    """
    txns = _dedupe_insider_txns(history)
    window_start = asof - timedelta(days=NETFLOW_WINDOW_DAYS)

    in_window = []
    for t in txns:
        d = _parse_date(t.get("date"))
        if d is None:
            continue
        if window_start <= d <= asof:
            in_window.append((d, t))

    buy_usd = 0.0
    sell_usd = 0.0
    for _, t in in_window:
        action = t.get("action")
        if action in NETFLOW_EXCLUDE:
            continue
        try:
            val = float(t.get("value_usd") or 0)
        except (TypeError, ValueError):
            val = 0.0
        if action == "Buy":
            buy_usd += val
        elif action == "Sell":
            sell_usd += val
    net_flow = buy_usd - sell_usd

    breaches = [t for _, t in in_window if t.get("breach") and t.get("action") not in NETFLOW_EXCLUDE]
    senior_breaches = [t for t in breaches if role_is_senior(t.get("role"))]

    # cluster: ≥3 distinct insiders, same direction, within any ~5d span
    cluster, cluster_dir = 0, None
    for direction in ("Sell", "Buy"):
        dated = sorted(
            ((d, t.get("insider_name")) for d, t in in_window
             if t.get("action") == direction and t.get("insider_name")),
            key=lambda x: x[0],
        )
        # sliding window over dates
        for i in range(len(dated)):
            names = set()
            for j in range(i, len(dated)):
                if (dated[j][0] - dated[i][0]).days > CLUSTER_WINDOW_DAYS:
                    break
                names.add(dated[j][1])
            if len(names) >= 3 and len(names) > cluster:
                cluster, cluster_dir = len(names), direction

    return {
        "net_flow": net_flow,
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        "window_txns": [t for _, t in in_window],
        "all_txns": txns,
        "breaches": breaches,
        "senior_breaches": senior_breaches,
        "cluster": cluster,
        "cluster_dir": cluster_dir,
    }


# ===========================================================================
# COPIED helpers — bytewise from render_macro_tracker.py
# ===========================================================================
def load_macro_snapshot(vault_root: Path):
    path = vault_root / "raw" / "stockdog" / "macro" / "macro_snapshot.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _col(daily, key):
    """Column extraction preserving order; daily is oldest->newest."""
    return [row.get(key) for row in daily]


def _last_valid(vals):
    for v in reversed(vals):
        if v is not None:
            return v
    return None


def _nth_back_valid(vals, n):
    """Value n valid-points back from latest (0 = latest); None if unavailable."""
    valid = [v for v in vals if v is not None]
    if not valid:
        return None
    idx = len(valid) - 1 - n
    return valid[idx] if 0 <= idx < len(valid) else None


def _last_valid_date(daily, keys):
    """Date of the most recent daily row where ANY of `keys` is non-null."""
    for row in reversed(daily):
        if any(row.get(k) is not None for k in keys):
            return row.get("date")
    return None


# ===========================================================================
# COPIED helpers — bytewise from render_watchlist_tracker.py
# ===========================================================================
def _is_num(v) -> bool:
    """True for a real finite number (excludes None / NaN / non-numeric)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f  # False for NaN


def load_watchlist_snapshot(vault_root: Path):
    path = vault_root / "raw" / "stockdog" / "watchlist" / "watchlist_snapshot.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ===========================================================================
# signals-specific helpers
# ===========================================================================
def _money_short(v):
    """Compact signed $ — e.g. +$3.2M / −$850K / $0."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if n > 0 else ("-" if n < 0 else "")
    a = abs(n)
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:.0f}K"
    return f"{sign}${a:.0f}"


def _signed_pp(v, dp=1):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):.{dp}f}%p"


def _signed_bps(v, dp=0):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):.{dp}f}bps"


def _signed_pct(v, dp=1):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):.{dp}f}%"


def _zone_index(score):
    """Index 0..4 of the F&G zone a score falls in (boundaries FG_ZONE_BOUNDS)."""
    if score is None:
        return None
    for i, b in enumerate(FG_ZONE_BOUNDS):
        if score <= b:
            return i
    return len(FG_ZONE_BOUNDS)


def make_flag(domain, ticker, kind, tier, text, *,
              state_changed=False, threshold_excess=0, rarity=False,
              leverage_solo=False, asof=None, source="", static=False):
    """Build a normalized flag dict + compute its score."""
    base = TIER_BASE.get(tier, 1)
    score = (base
             + (EVENT_BONUS if state_changed else 0)
             + max(0, min(2, threshold_excess))
             + (RARITY_BONUS if rarity else 0)
             - (LEVERAGE_PENALTY if leverage_solo else 0))
    return {
        "domain": domain,        # short|insider|macro|watchlist|fg
        "ticker": ticker,        # ticker / indicator name (for cross-signal join)
        "kind": kind,
        "tier": tier,
        "text": text,            # "무엇(수치)" body — no 종목 prefix, no 출처/기준일
        "state_changed": state_changed,
        "static": static,        # static multi-day state → banner-only, never main list
        "score": score,
        "source": source,
        "asof": asof or "—",
        "consumed": False,       # set True when bundled into a cross-signal card
    }


# ===========================================================================
# flag collectors — each returns (flags, freshness_date); None-safe
# ===========================================================================
def collect_short(short_data, vault_root):
    flags = []
    by_ticker = (short_data or {}).get("by_ticker", {}) or {}
    if not short_data or not by_ticker:
        return [], None
    freshness = (short_data or {}).get("file_used") or (short_data or {}).get("date")
    above = 0
    valid = 0
    for tk in TICKERS:
        cur = by_ticker.get(tk) or {}
        if cur.get("error"):
            continue
        hist = load_short_history(vault_root, tk)  # newest-first
        recent = hist[:SHORT_SPARK_DAYS]
        ratios_new_first = [r.get("short_ratio") for r in recent if r.get("short_ratio") is not None]
        ratios_oldest = list(reversed(ratios_new_first))
        n = len(ratios_oldest)
        cur_ratio = cur.get("short_ratio")
        if cur_ratio is None and ratios_new_first:
            cur_ratio = ratios_new_first[0]
        m = (sum(ratios_oldest) / n) if n else None
        dev = (cur_ratio - m) if (cur_ratio is not None and m is not None) else None
        if dev is None:
            continue
        valid += 1
        if dev > 0:
            above += 1
        drift = drift_flag(ratios_oldest)
        building = " (~%dd, building)" % n if n < BUILDING_THRESHOLD else ""
        # deviation flag (static-ish): base tier C, state_changed False.
        if abs(dev) >= SHORT_DEV_PP:
            excess = 0
            if abs(dev) >= SHORT_DEV_EXCESS2:
                excess = 2
            elif abs(dev) >= SHORT_DEV_EXCESS1:
                excess = 1
            flags.append(make_flag(
                "short", tk, "dev", "C",
                f"공매도 비중 {cur_ratio:.1f}% — 평균 대비 {_signed_pp(dev)}{building}",
                state_changed=False, threshold_excess=excess,
                asof=cur.get("data_as_of") or freshness,
                source="FINRA RegSHO",
            ))
        # drift flag (fresh run event): tier C, state_changed True.
        if drift in ("3d↑", "3d↓"):
            flags.append(make_flag(
                "short", tk, "drift", "C",
                f"공매도 비중 {drift} 드리프트 ({cur_ratio:.1f}%){building}",
                state_changed=True,
                asof=cur.get("data_as_of") or freshness,
                source="FINRA RegSHO",
            ))
    # breadth → context line only (carried as a static, non-main flag)
    if valid and above >= SHORT_BREADTH:
        flags.append(make_flag(
            "short", "*breadth*", "breadth_static", "C",
            f"M7 {above}/{valid} 종목이 공매도 비중 평균 상회 — 광범위 관찰",
            static=True, asof=freshness, source="FINRA RegSHO",
        ))
    return flags, freshness


def collect_insider(insider_data, vault_root, asof: _date):
    flags = []
    by_ticker = (insider_data or {}).get("by_ticker", {}) or {}
    if not insider_data or not by_ticker:
        return [], None
    asof_date = (insider_data or {}).get("date") or asof.isoformat()
    for tk in TICKERS:
        hist = load_insider_history(vault_root, tk)
        if not hist:
            continue
        summ = compute_insider_summary(hist, asof)
        if not summ["window_txns"]:
            continue
        # breach
        if summ["senior_breaches"]:
            flags.append(make_flag(
                "insider", tk, "breach_senior", "A",
                f"고위직 내부자 임계 초과(breach) {len(summ['senior_breaches'])}건",
                rarity=True, asof=asof_date, source="SEC Form 4",
            ))
        elif summ["breaches"]:
            flags.append(make_flag(
                "insider", tk, "breach", "B",
                f"내부자 임계 초과(breach) {len(summ['breaches'])}건",
                asof=asof_date, source="SEC Form 4",
            ))
        # large net-flow
        nf = summ["net_flow"]
        if abs(nf) >= INSIDER_NETFLOW_USD:
            excess = 0
            if abs(nf) >= INSIDER_NETFLOW_EXCESS2:
                excess = 2
            elif abs(nf) >= INSIDER_NETFLOW_EXCESS1:
                excess = 1
            flags.append(make_flag(
                "insider", tk, "netflow", "B",
                f"순매수액(~{NETFLOW_WINDOW_DAYS}d) {_money_short(nf)} "
                f"(매수 {_money_short(summ['buy_usd'])} / 매도 {_money_short(-summ['sell_usd'])})",
                threshold_excess=excess, asof=asof_date, source="SEC Form 4",
            ))
        # cluster
        if summ["cluster"] >= INSIDER_CLUSTER:
            dir_kr = "매도" if summ["cluster_dir"] == "Sell" else "매수"
            flags.append(make_flag(
                "insider", tk, "cluster", "A",
                f"클러스터 — ~{CLUSTER_WINDOW_DAYS}일 내 동일 방향 내부자 ×{summ['cluster']} ({dir_kr})",
                rarity=True, asof=asof_date, source="SEC Form 4",
            ))
    return flags, asof_date


def collect_macro(macro_snap, asof: _date):
    flags = []
    if not macro_snap:
        return [], None
    daily = macro_snap.get("daily") or []
    if not daily:
        return [], None
    freshness = _last_valid_date(daily, ("us_2y", "macro_10y", "us_30y"))

    spread = _col(daily, "t10y2y")
    spread_valid = [v for v in spread if v is not None]
    spread_latest = _last_valid(spread)
    spread_prior = spread_valid[-2] if len(spread_valid) >= 2 else None

    # inversion (t10y2y < 0)
    if spread_latest is not None and spread_latest < 0:
        sign_flip = (spread_prior is not None and spread_prior >= 0)
        if sign_flip:
            flags.append(make_flag(
                "macro", "10Y-2Y", "inversion_flip", "A",
                f"10Y-2Y 스프레드 역전 진입 ({spread_latest:.2f}%) — 직전 정상에서 전환",
                state_changed=True, asof=freshness, source="FRED UST",
            ))
        else:
            flags.append(make_flag(
                "macro", "10Y-2Y", "inversion_static", "A",
                f"10Y-2Y 역전 지속 ({spread_latest:.2f}%)",
                static=True, asof=freshness, source="FRED UST",
            ))

    # yield Δ5d (10Y, 2Y)
    for label, key in (("10Y", "macro_10y"), ("2Y", "us_2y")):
        vals = _col(daily, key)
        latest = _last_valid(vals)
        d5 = _nth_back_valid(vals, 5)
        if latest is not None and d5 is not None:
            bps = (latest - d5) * 100
            if abs(bps) >= YIELD_BPS:
                excess = 0
                if abs(bps) >= YIELD_BPS * 2:
                    excess = 2
                elif abs(bps) >= YIELD_BPS * 1.5:
                    excess = 1
                flags.append(make_flag(
                    "macro", label, "yield", "B",
                    f"{label} 금리 Δ5d {_signed_bps(bps)} (현재 {latest:.2f}%)",
                    threshold_excess=excess, asof=freshness, source="FRED UST",
                ))

    # spread Δ5d
    sp_latest = _last_valid(spread)
    sp_5d = _nth_back_valid(spread, 5)
    if sp_latest is not None and sp_5d is not None:
        bps = (sp_latest - sp_5d) * 100
        if abs(bps) >= SPREAD_BPS:
            flags.append(make_flag(
                "macro", "10Y-2Y", "spread", "B",
                f"10Y-2Y 스프레드 Δ5d {_signed_bps(bps)} (현재 {sp_latest:.2f}%)",
                asof=freshness, source="FRED UST",
            ))

    # FOMC blackout onset
    upcoming = [d for d in FOMC_DATES if d >= asof.isoformat()]
    if upcoming:
        nxt = datetime.strptime(upcoming[0], "%Y-%m-%d").date()
        if (nxt - timedelta(days=FOMC_BLACKOUT_PRE)) <= asof <= (nxt + timedelta(days=FOMC_BLACKOUT_POST)):
            d_n = (nxt - asof).days
            flags.append(make_flag(
                "macro", "FOMC", "fomc_blackout", "A",
                f"FOMC 블랙아웃 기간 (다음 회의 {nxt.isoformat()}, D-{d_n})",
                state_changed=True, asof=asof.isoformat(), source="FOMC schedule",
            ))
    return flags, freshness


def collect_watchlist(wl_snap):
    flags = []
    if not wl_snap:
        return [], None
    tickers = wl_snap.get("tickers") or {}
    if not tickers:
        return [], None
    freshness = wl_snap.get("updated")
    # collect per-ticker so leverage-solo penalty can see proximity peers
    for tk, info in tickers.items():
        if not info:
            continue
        history = info.get("history") or []
        latest = info.get("latest") or {}
        closes = [float(h.get("close")) for h in history if _is_num(h.get("close"))]
        vols = [float(h.get("volume")) for h in history if _is_num(h.get("volume"))]
        n = len(closes)
        if n == 0:
            continue
        price_v = float(latest["close"]) if _is_num(latest.get("close")) else closes[-1]
        vol_v = float(latest["volume"]) if _is_num(latest.get("volume")) else (vols[-1] if vols else None)

        # 거래량 vs 평균 ratio
        vol_ratio = None
        if len(vols) >= 5 and vol_v is not None:
            vw = vols[-VOL_WINDOW:]
            vm = mean(vw) if vw else None
            if vm not in (None, 0):
                vol_ratio = vol_v / vm

        # proximity flag presence (need ≥ BUILDING_THRESHOLD)
        prox_kind = None
        if n >= BUILDING_THRESHOLD and price_v is not None:
            hi, lo = max(closes), min(closes)
            if hi not in (None, 0) and abs(price_v - hi) / hi <= PROX:
                prox_kind = ("high", hi)
            elif lo not in (None, 0) and abs(price_v - lo) / lo <= PROX:
                prox_kind = ("low", lo)

        # Δ5d
        d5 = None
        if n >= D5_LOOKBACK + 1 and closes[-1 - D5_LOOKBACK] not in (None, 0):
            d5 = (closes[-1] - closes[-1 - D5_LOOKBACK]) / closes[-1 - D5_LOOKBACK] * 100.0

        # 거래량▲
        if vol_ratio is not None and vol_ratio >= VOL_SPIKE_MULT:
            excess = 0
            if vol_ratio >= VOL_EXCESS2:
                excess = 2
            elif vol_ratio >= VOL_EXCESS1:
                excess = 1
            leverage_solo = (tk in LEVERAGED and prox_kind is None)
            flags.append(make_flag(
                "watchlist", tk, "volume", "B",
                f"거래량 {vol_ratio:.1f}× (20일 평균 대비)",
                threshold_excess=excess, leverage_solo=leverage_solo,
                asof=latest.get("date") or freshness, source="yfinance",
            ))
        # 고/저 근접
        if prox_kind is not None:
            side = "고점" if prox_kind[0] == "high" else "저점"
            flags.append(make_flag(
                "watchlist", tk, "proximity", "B",
                f"{n}일 {side} 근접 (현재 {price_v:.2f})",
                asof=latest.get("date") or freshness, source="yfinance",
            ))
        # Δ5d |%| ≥ 8
        if d5 is not None and abs(d5) >= WL_D5_PCT:
            flags.append(make_flag(
                "watchlist", tk, "d5", "C",
                f"5일 변동 {_signed_pct(d5)} (현재 {price_v:.2f})",
                asof=latest.get("date") or freshness, source="yfinance",
            ))
    return flags, freshness


def collect_fg(metrics_snap):
    flags = []
    if not metrics_snap:
        return [], None
    series = metrics_snap.get("series") or []
    if not series:
        return [], None
    freshness = metrics_snap.get("updated")
    fg_vals = [r.get("fg_score") for r in series]
    fg_valid = [v for v in fg_vals if v is not None]
    if len(fg_valid) < 2:
        return [], freshness
    latest = fg_valid[-1]
    prior = fg_valid[-2]
    delta = latest - prior
    # 1d jump
    if abs(delta) >= FG_JUMP:
        excess = 1 if abs(delta) >= FG_JUMP_EXCESS1 else 0
        flags.append(make_flag(
            "fg", "F&G", "fg_jump", "C",
            f"Fear & Greed 1d 변동 {delta:+.0f} ({prior:.0f}→{latest:.0f}, {fg_zone(latest)})",
            threshold_excess=excess, asof=freshness, source="CNN F&G",
        ))
    # regime boundary cross
    zi_now = _zone_index(latest)
    zi_prev = _zone_index(prior)
    if zi_now is not None and zi_prev is not None and zi_now != zi_prev:
        flags.append(make_flag(
            "fg", "F&G", "fg_regime", "B",
            f"Fear & Greed 국면 전환 {fg_zone(prior)}→{fg_zone(latest)} ({prior:.0f}→{latest:.0f})",
            state_changed=True, asof=freshness, source="CNN F&G",
        ))
    return flags, freshness


# ===========================================================================
# cross-signals
# ===========================================================================
def _has(flags, domain, ticker, kinds):
    return [f for f in flags if f["domain"] == domain and f["ticker"] == ticker
            and f["kind"] in kinds and not f["consumed"]]


def build_cross_signals(flags):
    """Detect CS-1/CS-2/CS-3; mark consumed legs; return list of CS card dicts."""
    cards = []

    # CS-1 (same M7 ticker): (insider breach OR cluster) AND short_dev > 0 (above mean)
    for tk in TICKERS:
        ins = [f for f in flags if f["domain"] == "insider" and f["ticker"] == tk
               and f["kind"] in ("breach", "breach_senior", "cluster") and not f["consumed"]]
        sh = [f for f in flags if f["domain"] == "short" and f["ticker"] == tk
              and f["kind"] == "dev" and not f["consumed"]
              and "평균 대비 +" in f["text"]]  # above-mean only
        if ins and sh:
            legs = ins + sh
            for f in legs:
                f["consumed"] = True
            score = max(l["score"] for l in legs) + CS_BONUS
            body = "; ".join(l["text"] for l in legs)
            cards.append({
                "cs": "CS-1", "ticker": tk, "score": score,
                "text": f"{tk} — 내부자 이벤트 + 공매도 비중 평균 상회 동시 발생 · {body}",
                "asof": legs[0]["asof"], "source": "SEC Form 4 + FINRA RegSHO",
            })

    # CS-2 (same watchlist ticker): 거래량▲ AND 고/저 근접
    wl_tickers = sorted({f["ticker"] for f in flags if f["domain"] == "watchlist"})
    for tk in wl_tickers:
        vol = _has(flags, "watchlist", tk, ("volume",))
        prox = _has(flags, "watchlist", tk, ("proximity",))
        if vol and prox:
            legs = vol + prox
            for f in legs:
                f["consumed"] = True
            score = max(l["score"] for l in legs) + CS_BONUS
            body = "; ".join(l["text"] for l in legs)
            cards.append({
                "cs": "CS-2", "ticker": tk, "score": score,
                "text": f"{tk} — 거래량 급증 + 고/저 근접 동시 · {body}",
                "asof": legs[0]["asof"], "source": "yfinance",
            })

    # CS-3: any macro yield-Δ flag AND any RATE_SENSITIVE name has drift / 거래량▲ / |Δ5d|≥8
    macro_yield = [f for f in flags if f["domain"] == "macro"
                   and f["kind"] in ("yield", "spread") and not f["consumed"]]
    if macro_yield:
        rs_moves = []
        for f in flags:
            if f["consumed"]:
                continue
            if f["ticker"] in RATE_SENSITIVE and (
                (f["domain"] == "short" and f["kind"] == "drift")
                or (f["domain"] == "watchlist" and f["kind"] in ("volume", "d5"))
            ):
                rs_moves.append(f)
        if rs_moves:
            legs = macro_yield + rs_moves
            for f in legs:
                f["consumed"] = True
            score = max(l["score"] for l in legs) + CS_BONUS
            mleg = "; ".join(l["text"] for l in macro_yield)
            rleg = "; ".join(f"{l['ticker']}: {l['text']}" for l in rs_moves)
            cards.append({
                "cs": "CS-3", "ticker": "(매크로×금리민감)", "score": score,
                "text": f"매크로 금리 변동 + 금리민감 종목 동반 움직임 (정성 관찰, 인과 아님) · {mleg} ‖ {rleg}",
                "asof": macro_yield[0]["asof"], "source": "FRED UST + 워치리스트/M7",
            })

    return cards


# ===========================================================================
# read-block preserve
# ===========================================================================
READ_BLOCK_RE = re.compile(
    r"<!--\s*TODAY_READ:START\s+(\d{4}-\d{2}-\d{2})\s*-->.*?<!--\s*TODAY_READ:END\s*-->",
    re.DOTALL,
)


def extract_read_block(out_path: Path, today: str):
    """Return the existing TODAY_READ block text iff it is dated == today, else None.
    Stale-dated blocks are dropped (returns None)."""
    if not out_path.is_file():
        return None
    try:
        existing = out_path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = READ_BLOCK_RE.search(existing)
    if not m:
        return None
    if m.group(1) == today:
        return m.group(0)
    return None


# ===========================================================================
# rendering
# ===========================================================================
def _flag_line(f):
    """주요/관찰 list line: 종목/지표 — 무엇(수치) ·출처 ·기준일."""
    return f"- **{f['ticker']}** — {f['text']} ·{f['source']} ·{f['asof']}"


def _cs_line(card):
    return f"- **[{card['cs']}]** {card['text']} ·{card['source']} ·{card['asof']}"


def _write_signals_archive(vault_root: Path, run_date: str, major: int, watch: int, notable: bool, today_read: str = None) -> None:
    """IMPR-067 ongoing archive write — record signal counts + read for every day.

    Writes raw/stockdog/signals/archive/<date>.json with:
      {date, today_read, major, watch, notable}

    Called from render_signals_tracker.py for ALL days (even quiet ones).
    On notable days with LLM read, inject_today_read.py will overwrite with
    the confirmed read text. On quiet days (notable=false), today_read=null.

    Idempotent: same date overwrites. Non-fatal: archive write failure
    must never break the render flow.
    """
    try:
        archive_dir = vault_root / "raw" / "stockdog" / "signals" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        archive_entry = {
            "date": run_date,
            "today_read": today_read,  # null on quiet days, or confirmed text on notable days
            "major": major,
            "watch": watch,
            "notable": notable,
        }

        archive_path = archive_dir / f"{run_date}.json"
        archive_json = json.dumps(archive_entry, ensure_ascii=False, indent=2)

        # Atomic write (temp + os.replace)
        tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
        tmp_path.write_text(archive_json, encoding="utf-8")
        os.replace(tmp_path, archive_path)

    except Exception as e:
        # Non-fatal: archive write failure must never break the render.
        print(f"[render_signals_tracker] archive write failed ({e}) — continuing", file=sys.stderr)


def compute_scored_flags(vault_root, asof=None):
    """Pure: load snapshots + run all collectors + cross-signals. No file writes, no print.

    Returns dict with keys:
      - flags: list of all individual flags (static + live)
      - static_flags: list of static flags (banner-only)
      - live_flags: list of live flags (main list candidates)
      - cs_cards: list of cross-signal cards
      - freshness: dict of {domain: freshness_date}
      - domain_present: dict of {domain: bool}
      - asof: the as-of date (date object)

    Single source of truth for severity scoring — imported by generate_preview_story.py (IMPR-076).
    Raises ValueError if no snapshots available.
    """
    if asof is None:
        asof = _date.today()
    want_date = asof.isoformat()

    short_data = load_category(vault_root, "short", want_date)
    insider_data = load_category(vault_root, "insider", want_date)
    macro_snap = load_macro_snapshot(vault_root)
    wl_snap = load_watchlist_snapshot(vault_root)
    metrics_snap = load_metrics_snapshot(vault_root)

    domain_present = {
        "short": bool(short_data and (short_data.get("by_ticker") or {})),
        "insider": bool(insider_data and (insider_data.get("by_ticker") or {})),
        "macro": bool(macro_snap and (macro_snap.get("daily") or [])),
        "watchlist": bool(wl_snap and (wl_snap.get("tickers") or {})),
        "fg": bool(metrics_snap and (metrics_snap.get("series") or [])),
    }
    if not any(domain_present.values()):
        raise ValueError(f"no snapshots available for {want_date}")

    short_flags, short_fresh = collect_short(short_data, vault_root)
    insider_flags, insider_fresh = collect_insider(insider_data, vault_root, asof)
    macro_flags, macro_fresh = collect_macro(macro_snap, asof)
    wl_flags, wl_fresh = collect_watchlist(wl_snap)
    fg_flags, fg_fresh = collect_fg(metrics_snap)

    all_flags = short_flags + insider_flags + macro_flags + wl_flags + fg_flags
    freshness = {
        "short": short_fresh, "insider": insider_fresh, "macro": macro_fresh,
        "watchlist": wl_fresh, "fg": fg_fresh,
    }

    # static states (banner-only, never main list) split out before scoring lists
    static_flags = [f for f in all_flags if f["static"]]
    live_flags = [f for f in all_flags if not f["static"]]

    cs_cards = build_cross_signals(live_flags)

    return {
        "flags": all_flags,
        "static_flags": static_flags,
        "live_flags": live_flags,
        "cs_cards": cs_cards,
        "freshness": freshness,
        "domain_present": domain_present,
        "asof": asof,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: render_signals_tracker.py <vault_root> [<date>]", file=sys.stderr)
        return 1

    vault_root = Path(sys.argv[1]).expanduser().resolve()
    want_date = sys.argv[2] if len(sys.argv) > 2 else _date.today().strftime("%Y-%m-%d")
    asof = _parse_date(want_date) or _date.today()
    run_date = asof.isoformat()

    try:
        result = compute_scored_flags(vault_root, asof=asof)
    except ValueError:
        print(f"[render_signals_tracker] no snapshots available for {want_date} — skipping write.")
        return 2

    # Unpack result dict
    all_flags = result["flags"]
    static_flags = result["static_flags"]
    live_flags = result["live_flags"]
    cs_cards = result["cs_cards"]
    freshness = result["freshness"]
    domain_present = result["domain_present"]

    # remaining standalone flags (cross-consumed removed)
    standalone = [f for f in live_flags if not f["consumed"]]

    # major = score >= 6 (standalone) + all CS cards (CS card score already ≥ base+3)
    major_flags = [f for f in standalone if f["score"] >= SCORE_MAJOR]
    watch_flags = [f for f in standalone if SCORE_WATCH <= f["score"] < SCORE_MAJOR]
    dim_flags = [f for f in standalone if f["score"] < SCORE_WATCH]

    major_cs = [c for c in cs_cards if c["score"] >= SCORE_MAJOR]
    watch_cs = [c for c in cs_cards if SCORE_WATCH <= c["score"] < SCORE_MAJOR]

    major_flags.sort(key=lambda f: -f["score"])
    watch_flags.sort(key=lambda f: -f["score"])

    # ---- per-tracker summary counts (major/watch/dim) on standalone + CS attribution
    def _domain_of_cs(card):
        return {"CS-1": "insider", "CS-2": "watchlist", "CS-3": "macro"}[card["cs"]]

    summary = {d: {"major": 0, "watch": 0, "dim": 0} for d in
               ("short", "insider", "macro", "watchlist", "fg")}
    for f in standalone:
        if f["score"] >= SCORE_MAJOR:
            summary[f["domain"]]["major"] += 1
        elif f["score"] >= SCORE_WATCH:
            summary[f["domain"]]["watch"] += 1
        else:
            summary[f["domain"]]["dim"] += 1
    for c in cs_cards:
        d = _domain_of_cs(c)
        if c["score"] >= SCORE_MAJOR:
            summary[d]["major"] += 1
        elif c["score"] >= SCORE_WATCH:
            summary[d]["watch"] += 1

    # ---- context banner (static) values
    # Need to reload snapshots for banner context (they were consumed during compute_scored_flags)
    macro_snap = load_macro_snapshot(vault_root)
    metrics_snap = load_metrics_snapshot(vault_root)

    fg_series = (metrics_snap or {}).get("series") or []
    fg_latest = _last_valid([r.get("fg_score") for r in fg_series]) if fg_series else None
    fg_txt = f"{int(round(fg_latest))}({fg_zone(fg_latest)})" if fg_latest is not None else "—"
    # FOMC D-N
    upcoming = [d for d in FOMC_DATES if d >= run_date]
    if upcoming:
        nxt = datetime.strptime(upcoming[0], "%Y-%m-%d").date()
        fomc_txt = f"D-{(nxt - asof).days}"
    else:
        fomc_txt = "일정 미공개"
    # spread
    macro_daily = (macro_snap or {}).get("daily") or []
    spread_latest = _last_valid(_col(macro_daily, "t10y2y")) if macro_daily else None
    if spread_latest is None:
        spread_txt = "—"
    elif spread_latest < 0:
        spread_txt = f"{spread_latest * 100:+.0f}bps(역전)"
    else:
        spread_txt = f"+{spread_latest * 100:.0f}bps(정상)"
    banner = f"F&G: {fg_txt} · FOMC {fomc_txt} · 10Y-2Y: {spread_txt}"

    # ---- read-block preserve
    out_dir = vault_root / "10_Public" / "trackers"
    out_path = out_dir / "signals.md"
    preserved = extract_read_block(out_path, run_date)
    if preserved:
        read_block = preserved
    else:
        read_block = f"<!-- TODAY_READ:START {run_date} -->\n<!-- TODAY_READ:END -->"

    # ---- compose
    L = []
    L.append("---")
    L.append('title: "오늘의 시그널 — 트래커 종합"')
    L.append("public: true")
    L.append("type: reference")
    L.append(f"date: {run_date}")
    L.append("tags:")
    L.append("  - ctx/public")
    L.append("  - stockdog")
    L.append("  - signals")
    L.append("  - tracker")
    L.append("  - region/us")
    L.append("---")
    L.append("")
    L.append("# 오늘의 시그널 — 트래커 종합")
    L.append("")
    L.append(
        "> M7(공매도·내부자) · 매크로 · 워치리스트 · Fear & Greed 트래커의 "
        "**임계 초과 관찰**만 한곳에 모아 점수화합니다. 동일 스냅샷을 읽기 전용으로 "
        "재집계한 페이지이며, **매매 시그널이 아닙니다** — 모든 항목은 관찰입니다."
    )
    L.append("")
    L.append(f"**컨텍스트(정적):** {banner}")
    L.append("*정적 컨텍스트이며 시그널이 아닙니다.*")
    L.append("")
    L.append(read_block)
    L.append("")

    # static banner states (page-worthy gate: inversion_static / breadth_static …)
    if static_flags:
        L.append("> [!info] 정적 다일 상태 (배너 — 메인 리스트 제외)")
        for f in static_flags:
            L.append(f"> - {f['text']} ·{f['source']} ·{f['asof']}")
        L.append("")

    # 🔴 주요
    L.append("## 🔴 주요 시그널")
    L.append("")
    if major_cs or major_flags:
        for c in sorted(major_cs, key=lambda c: -c["score"]):
            L.append(_cs_line(c))
        for f in major_flags:
            L.append(_flag_line(f))
    else:
        L.append("오늘 주요 시그널 없음.")
    L.append("")

    # 🟡 관찰
    L.append("## 🟡 관찰")
    L.append("")
    watch_all = ([("cs", c) for c in watch_cs] + [("f", f) for f in watch_flags])
    watch_all.sort(key=lambda x: -(x[1]["score"]))
    if watch_all:
        shown = watch_all[:WATCH_CAP]
        overflow = len(watch_all) - len(shown)
        for kind, item in shown:
            L.append(_cs_line(item) if kind == "cs" else _flag_line(item))
        if overflow > 0:
            L.append(f"- +{overflow}건 (요약 참조)")
    else:
        L.append("관찰 시그널 없음.")
    L.append("")

    # 📊 트래커별 요약
    L.append("## 📊 트래커별 요약")
    L.append("")
    L.append("| 트래커 | 주요 | 관찰 | dim | 기준일 |")
    L.append("| --- | --- | --- | --- | --- |")
    label_map = [
        ("short", "M7 공매도"),
        ("insider", "M7 내부자"),
        ("macro", "매크로"),
        ("watchlist", "워치리스트"),
        ("fg", "Fear & Greed"),
    ]
    for d, label in label_map:
        if not domain_present[d]:
            L.append(f"| {label} | — | — | — | 스냅샷 없음 |")
            continue
        s = summary[d]
        fr = freshness.get(d) or "—"
        L.append(f"| {label} | {s['major']} | {s['watch']} | {s['dim']} | {fr} |")
    L.append("")

    # ℹ️ 방법론
    L.append("## ℹ️ 방법론")
    L.append("")
    L.append("**임계값 (초기 추정값 — 30일 후 재보정, IMPR-063 follow-up):**")
    L.append("")
    L.append(f"- 공매도 비중 편차: |현재 − history 평균| ≥ {SHORT_DEV_PP:.0f}%p "
             f"(초과 가산 ≥{SHORT_DEV_EXCESS1:.0f} +1, ≥{SHORT_DEV_EXCESS2:.0f} +2); "
             f"3일 드리프트; {SHORT_BREADTH}/7 이상 상회 시 광범위 컨텍스트.")
    L.append(f"- 내부자 순매수액: |net flow| ≥ ${INSIDER_NETFLOW_USD // 1_000_000}M "
             f"(≥${INSIDER_NETFLOW_EXCESS1 // 1_000_000}M +1, ≥${INSIDER_NETFLOW_EXCESS2 // 1_000_000}M +2); "
             f"breach(임계 초과, 고위직=희소); 클러스터 ~{CLUSTER_WINDOW_DAYS}일 내 동일 방향 ≥{INSIDER_CLUSTER}명. "
             f"윈도우 {NETFLOW_WINDOW_DAYS}일, 공개시장 Buy−Sell만 (TaxWithholding·Gift·Grant·Exercise 제외).")
    L.append(f"- 금리: 10Y/2Y Δ5d |{YIELD_BPS:.0f}bps| 이상 (×1.5 +1, ×2 +2); "
             f"10Y-2Y 스프레드 Δ5d |{SPREAD_BPS:.0f}bps| 이상; "
             f"10Y-2Y < 0 역전(전환=이벤트, 지속=배너); FOMC 블랙아웃 [회의−{FOMC_BLACKOUT_PRE}일, +{FOMC_BLACKOUT_POST}일].")
    L.append(f"- 워치리스트: 거래량 ≥ {VOL_SPIKE_MULT:.0f}× 20일 평균 (≥{VOL_EXCESS1:.0f}× +1, ≥{VOL_EXCESS2:.0f}× +2); "
             f"고/저 근접 ±{PROX * 100:.0f}% (n≥{BUILDING_THRESHOLD}); Δ5d |{WL_D5_PCT:.0f}%| 이상. "
             "레버리지 ETF 단독 거래량▲(근접 동반 없음)은 −2 페널티(decay 노이즈).")
    L.append(f"- Fear & Greed: 1d |Δ{FG_JUMP:.0f}| 이상 (≥{FG_JUMP_EXCESS1:.0f} +1); "
             f"국면 경계({'/'.join(str(b) for b in FG_ZONE_BOUNDS)}) 교차 시 전환 이벤트.")
    L.append("")
    L.append(f"**점수:** 기본(🔴A {TIER_BASE['A']} / B {TIER_BASE['B']} / C {TIER_BASE['C']}) "
             f"+ 이벤트(상태 전환 +{EVENT_BONUS}) + 초과(0~2) + 희소(+{RARITY_BONUS}) − 레버리지(−{LEVERAGE_PENALTY}). "
             f"교차 카드 = max(구성 점수)+{CS_BONUS}. "
             f"🔴 주요 ≥{SCORE_MAJOR} · 🟡 관찰 {SCORE_WATCH}~{SCORE_MAJOR - 1} · dim <{SCORE_WATCH}(집계만). "
             f"관찰은 상위 {WATCH_CAP}건 표시, 초과분은 요약 참조.")
    L.append("")
    L.append("**교차 시그널:** CS-1 동일 M7 종목 내부자 이벤트 + 공매도 평균 상회; "
             "CS-2 동일 워치리스트 종목 거래량▲ + 고/저 근접; "
             "CS-3 매크로 금리 변동 + 금리민감 종목 동반 움직임. "
             "**CS-3는 정성적 동시 관찰일 뿐 인과관계가 아닙니다.**")
    L.append("")
    L.append("- 정적 다일 상태(역전 지속, 광범위 상회 등)는 메인 리스트가 아닌 배너/요약으로만 표시합니다.")
    L.append("- 30일 미만 구간은 (~Nd, building)으로 표기 — 평균·편차 신뢰도가 낮습니다.")
    L.append("- 인플레이션 신규 발표(stateless) 플래그는 v1에서 제외 — 추후 추가 예정.")
    L.append("- **이 페이지는 관찰용이며 매매 시그널이 아닙니다.**")
    L.append("")

    L.append("")
    L.append(
        '<details class="dash-refs" open>\n'
        '<summary>출처 / References</summary>\n'
        '<div class="dash-refs-body">\n'
        '<p class="dash-refs-lede">데이터 출처 · 자동 생성 — 관찰용이며 매매 시그널이 아닙니다.</p>\n'
        '<a class="dash-ref-chip" href="https://www.sec.gov" target="_blank" rel="noopener">sec.gov</a>\n'
        '<a class="dash-ref-chip" href="https://www.finra.org" target="_blank" rel="noopener">finra.org</a>\n'
        '<a class="dash-ref-chip" href="https://fred.stlouisfed.org" target="_blank" rel="noopener">fred.stlouisfed.org</a>\n'
        '<a class="dash-ref-chip" href="https://finance.yahoo.com" target="_blank" rel="noopener">finance.yahoo.com</a>\n'
        '<a class="dash-ref-chip" href="https://www.cnn.com/markets/fear-and-greed" target="_blank" rel="noopener">cnn.com</a>\n'
        f'<p class="dash-refs-meta">자동 생성 · {run_date}</p>\n'
        '<p class="dash-refs-note">원본 트래커: <a href="/trackers/m7">M7</a> · <a href="/trackers/macro">매크로</a> · <a href="/trackers/watchlist">워치리스트</a></p>\n'
        '</div>\n'
        '</details>'
    )
    L.append("")

    out_dir.mkdir(parents=True, exist_ok=True)

    # Atomic write (temp file + os.replace) to handle root-owned files gracefully
    # and ensure new files are written as the running user.
    content = "\n".join(L)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, out_path)

    n_major = len(major_cs) + len(major_flags)
    n_watch = min(len(watch_all), WATCH_CAP)
    n_notable = (n_major + n_watch) > 0

    # IMPR-067 — emit gate sidecar for the automated "오늘의 읽기" generator.
    # generate_signals_read.py reads this to decide whether to call the LLM at all
    # (notable==false → ZERO LLM call). Non-fatal: a sidecar write failure must never
    # break the render, so OSError is swallowed (the generator then skips → safe).
    try:
        sig_dir = vault_root / "raw" / "stockdog" / "signals"
        sig_dir.mkdir(parents=True, exist_ok=True)
        gate_path = sig_dir / "signal_count.json"
        gate_content = json.dumps({
            "date": run_date,
            "major": n_major,
            "watch": n_watch,
            "notable": n_notable,
        })
        # Atomic write (temp + os.replace) for consistency
        gate_tmp = gate_path.with_suffix(gate_path.suffix + ".tmp")
        gate_tmp.write_text(gate_content, encoding="utf-8")
        os.replace(gate_tmp, gate_path)
    except OSError as e:
        print(f"[render_signals_tracker] gate sidecar write skipped (non-fatal): {e}", file=sys.stderr)

    # IMPR-067 ongoing — write archive entry for every day (counts + null read).
    # On notable days with LLM read, inject_today_read.py will overwrite with
    # confirmed text. Non-fatal: archive write failure must never break render.
    _write_signals_archive(vault_root, run_date, n_major, n_watch, n_notable, today_read=None)

    print(
        f"[render_signals_tracker] wrote {out_path} "
        f"(major={n_major}, watch={n_watch}, cs={len(cs_cards)}, "
        f"domains={sum(domain_present.values())}/5, read_block={'preserved' if preserved else 'placeholder'})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
