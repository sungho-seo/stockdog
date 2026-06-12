#!/usr/bin/env python3
"""Render the M7 (internal trades + short interest) public markdown tracker.

IMPR-058 Step 3 — stockdog side. Stdlib only (json, pathlib, sys, datetime).
Reads the aggregate dated M7 JSON files + per-ticker history + a staged metrics
snapshot, and writes a single public tracker page:
    <vault_root>/10_Public/trackers/m7.md

Enhancements (MVP synthesized from planner+analyst+stockdog ideation):
  A. Market regime strip (F&G / VIX / US 10Y + unicode sparklines)
  B. Short table: 추세 sparkline + 평균 대비(~Nd) + 드리프트, sorted by deviation,
     + breadth × VIX observational line.
  C. Insider: trailing-window net $ flow (Buy − Sell, open-market only),
     skip empty tickers, breach + cluster flag, role weighting.
  D. Honesty guards: "(~Nd, building)" labels under 30 days, no z-scores /
     percentiles / "X일 최고" / correlation claims.

raw/ is READ-ONLY — this script only reads from it and writes under 10_Public/.
The renderer does NOT read metrics_history.db (root-owned, gitignored); it reads
the staged raw/stockdog/m7/metrics_snapshot.json instead.

Usage:
    render_m7_tracker.py <vault_root> [<date>]   # date default = today (%Y-%m-%d)
"""

import json
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path

# Canonical ticker order — deterministic, NOT dict insertion order.
TICKERS = ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]

# Insider net-flow: open-market Buy/Sell only. EXCLUDE mechanical actions.
NETFLOW_EXCLUDE = {"TaxWithholding", "Gift", "Grant", "Exercise"}
NETFLOW_WINDOW_DAYS = 14  # trailing calendar-day window for net $ flow
CLUSTER_WINDOW_DAYS = 5   # ≥3 distinct insiders same dir within ~5d → cluster
SHORT_SPARK_DAYS = 13     # cap of short-ratio points used for sparkline/mean

SPARK_CHARS = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------
def load_category(vault_root: Path, category: str, want_date: str):
    """Load <vault_root>/raw/stockdog/m7/<category>/<want_date>.json.

    Falls back to the newest dated file (glob 2*.json) if the exact date is
    missing. Returns the parsed dict, or None if nothing usable exists.
    """
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


def load_json(path: Path):
    """Read a JSON file; None on absence / parse error."""
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_short_history(vault_root: Path, ticker: str) -> list:
    """Per-ticker short_history.json (newest-first list). [] if absent."""
    data = load_json(vault_root / "raw" / "stockdog" / "m7" / ticker / "short_history.json")
    return data if isinstance(data, list) else []


def load_insider_history(vault_root: Path, ticker: str) -> list:
    """Per-ticker insider_history.json (newest-first list of per-day snapshots)."""
    data = load_json(vault_root / "raw" / "stockdog" / "m7" / ticker / "insider_history.json")
    return data if isinstance(data, list) else []


def load_metrics_snapshot(vault_root: Path):
    """Staged metrics snapshot {updated, order, series:[...]}. None if absent."""
    return load_json(vault_root / "raw" / "stockdog" / "m7" / "metrics_snapshot.json")


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------
def comma_int(value) -> str:
    if value is None:
        return "—"
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return "—"
    return f"{n:,}"


def money(value) -> str:
    """'$' + thousands-comma int; '—' for 0/absent (e.g. Gift, TaxWithholding $0)."""
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n == 0:
        return "—"
    return f"${int(round(n)):,}"


def signed_money(value) -> str:
    """Signed '$' value with thousands-comma. '$0' for exactly zero."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if n > 0 else ("-" if n < 0 else "")
    return f"{sign}${abs(int(round(n))):,}"


def ratio_pct(value) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# analytics helpers (stdlib only)
# ---------------------------------------------------------------------------
def sparkline(values) -> str:
    """Unicode sparkline over a numeric series (already in display order).

    - Drops None entries.
    - Single point → single mid-char.
    - All-equal → repeat mid char (flat line).
    - Maps min..max onto ▁..█.
    """
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return ""
    mid = SPARK_CHARS[len(SPARK_CHARS) // 2]
    if len(nums) == 1:
        return mid
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return mid * len(nums)
    span = hi - lo
    out = []
    last = len(SPARK_CHARS) - 1
    for v in nums:
        idx = int(round((v - lo) / span * last))
        idx = max(0, min(last, idx))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def arrow(delta, eps=1e-9) -> str:
    """↑ / ↓ / → for a numeric delta (None → →)."""
    if delta is None:
        return "→"
    if delta > eps:
        return "↑"
    if delta < -eps:
        return "↓"
    return "→"


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


# ---------------------------------------------------------------------------
# A. market regime strip
# ---------------------------------------------------------------------------
def render_regime_section(snapshot) -> list:
    """Compact market-context strip from the staged metrics snapshot.

    Omitted entirely if the snapshot is absent or has no usable series.
    """
    if not snapshot:
        return []
    series = snapshot.get("series") or []
    # series is oldest->newest per the staging convention.
    if not series:
        return []

    def col(key):
        return [row.get(key) for row in series]

    fg_vals = col("fg_score")
    vix_vals = col("vix")
    y10_vals = col("us_10y")

    def last_valid(vals):
        for v in reversed(vals):
            if v is not None:
                return v
        return None

    def nth_back_valid(vals, n):
        """Return the value n valid-points back from the latest (0 = latest)."""
        valid = [v for v in vals if v is not None]
        if not valid:
            return None
        idx = len(valid) - 1 - n
        return valid[idx] if 0 <= idx < len(valid) else valid[0]

    fg_latest = last_valid(fg_vals)
    vix_latest = last_valid(vix_vals)
    y10_latest = last_valid(y10_vals)

    n_points = len(series)
    # VIX trend over available window (≈ up to N points), 10Y over ~5d.
    vix_first = nth_back_valid(vix_vals, n_points - 1)
    vix_arrow = arrow(None if (vix_latest is None or vix_first is None) else vix_latest - vix_first)
    y10_5d = nth_back_valid(y10_vals, 5)
    y10_arrow = arrow(None if (y10_latest is None or y10_5d is None) else y10_latest - y10_5d)

    lines = ["## 시장 컨텍스트", ""]
    updated = snapshot.get("updated", "—")
    lines.append(f"지표 스냅샷 기준 (~{n_points}d, building) · 갱신: {updated}")
    lines.append("")
    lines.append("| 지표 | 최신 | 추세 | 추이 |")
    lines.append("| --- | --- | --- | --- |")
    if fg_latest is not None:
        lines.append(
            f"| Fear & Greed | {int(round(fg_latest))} ({fg_zone(fg_latest)}) "
            f"| `{sparkline(fg_vals)}` | — |"
        )
    if vix_latest is not None:
        lines.append(
            f"| VIX | {vix_latest:.2f} | `{sparkline(vix_vals)}` "
            f"| {vix_arrow} (~{n_points}d) |"
        )
    if y10_latest is not None:
        lines.append(
            f"| US 10Y | {y10_latest:.3f}% | `{sparkline(y10_vals)}` "
            f"| {y10_arrow} (~5d) |"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# B. short table enhancement
# ---------------------------------------------------------------------------
def _vix_direction(snapshot):
    """Return ('↑'|'↓'|'평탄', latest_vix) from the snapshot, or (None, None)."""
    if not snapshot:
        return None, None
    series = snapshot.get("series") or []
    vix_vals = [r.get("vix") for r in series if r.get("vix") is not None]
    if len(vix_vals) < 2:
        return ("평탄", vix_vals[-1] if vix_vals else None)
    delta = vix_vals[-1] - vix_vals[0]
    if delta > 0.25:
        return "↑", vix_vals[-1]
    if delta < -0.25:
        return "↓", vix_vals[-1]
    return "평탄", vix_vals[-1]


def render_short_section(short_data, vault_root, snapshot) -> list:
    lines = []
    lines.append("## 공매도 비중 (FINRA RegSHO)")
    lines.append("")

    by_ticker = (short_data or {}).get("by_ticker", {}) or {}
    file_used = (short_data or {}).get("file_used", "—")
    freshness = (short_data or {}).get("freshness", "—")
    lines.append(f"데이터 기준일 · 신선도: {file_used} · {freshness}")
    lines.append("")

    # Build per-ticker rows with deviation, sparkline, drift.
    rows = []
    for tk in TICKERS:
        cur = by_ticker.get(tk) or {}
        if cur.get("error"):
            rows.append({"tk": tk, "error": True})
            continue
        hist = load_short_history(vault_root, tk)  # newest-first
        # newest-first → take last ~13 then reverse to oldest->newest for spark
        recent = hist[:SHORT_SPARK_DAYS]
        ratios_new_first = [r.get("short_ratio") for r in recent if r.get("short_ratio") is not None]
        ratios_oldest = list(reversed(ratios_new_first))
        n = len(ratios_oldest)
        # current ratio: prefer the dated aggregate, fall back to history head
        cur_ratio = cur.get("short_ratio")
        if cur_ratio is None and ratios_new_first:
            cur_ratio = ratios_new_first[0]
        mean = sum(ratios_oldest) / n if n else None
        dev = (cur_ratio - mean) if (cur_ratio is not None and mean is not None) else None
        rows.append({
            "tk": tk,
            "error": False,
            "cur_ratio": cur_ratio,
            "short_volume": cur.get("short_volume"),
            "total_volume": cur.get("total_volume"),
            "as_of": cur.get("data_as_of") or "—",
            "spark": sparkline(ratios_oldest),
            "mean": mean,
            "dev": dev,
            "n": n,
            "drift": drift_flag(ratios_oldest),
        })

    # Determine the actual N for the header (max history depth among valid rows).
    n_label = max((r.get("n", 0) for r in rows if not r.get("error")), default=0)
    lines.append(
        f"| 티커 | 공매도 비중 | 추세 (~{n_label}d) | 평균 대비(~{n_label}d) | 드리프트 "
        f"| 공매도량 | 총거래량 | 기준일 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")

    # Sort by absolute deviation desc (errors / missing-dev at the bottom).
    def sort_key(r):
        if r.get("error") or r.get("dev") is None:
            return (1, 0.0)
        return (0, -abs(r["dev"]))

    above = 0          # count of tickers above their own mean
    valid_dev = 0
    for r in sorted(rows, key=sort_key):
        tk = r["tk"]
        if r.get("error"):
            lines.append(f"| {tk} | — | — | — | — | — | — | — |")
            continue
        ratio = ratio_pct(r["cur_ratio"])
        spark = f"`{r['spark']}`" if r["spark"] else "—"
        if r["dev"] is None:
            dev_cell = "—"
        else:
            dev_cell = f"{'+' if r['dev'] >= 0 else '-'}{abs(r['dev']):.1f}%p"
            valid_dev += 1
            if r["dev"] > 0:
                above += 1
        lines.append(
            f"| {tk} | {ratio} | {spark} | {dev_cell} | {r['drift']} "
            f"| {comma_int(r['short_volume'])} | {comma_int(r['total_volume'])} | {r['as_of']} |"
        )
    lines.append("")
    lines.append(
        f"*평균 대비 = 현재 공매도 비중 − 가용 history 평균 (~{n_label}d, building). "
        "드리프트 = 최근 3개 포인트 단조 추세(≥3일)만 표시.*"
    )
    lines.append("")

    # breadth × VIX observational line
    vix_dir, vix_latest = _vix_direction(snapshot)
    breadth_line = f"**기준선 상회 {above}/{valid_dev}**" if valid_dev else "**기준선 상회 —**"
    if vix_dir is not None:
        vix_txt = {"↑": "VIX↑", "↓": "VIX↓", "평탄": "VIX 평탄"}[vix_dir]
        breadth_line += f" · {vix_txt}"
        if valid_dev and above >= 5 and vix_dir == "↑":
            read = "≥5/7 상회 + VIX↑ = 광범위 디리스킹 관찰"
        elif valid_dev and above >= 5:
            read = "광범위 상회 + VIX 비상승 = 종목 특이성 관찰"
        elif valid_dev:
            read = "상회 제한적 + VIX 평탄 = 종목 특이 관찰"
        else:
            read = "관찰 데이터 부족"
        breadth_line += f" → {read}"
    lines.append(breadth_line + " (시그널 아님, 관찰)")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# C. insider enhancement
# ---------------------------------------------------------------------------
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


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _is_real_insider_txn(t) -> bool:
    """True iff a transaction is a real open-market trade for DISPLAY purposes:
    positive $ value AND not a mechanical action (TaxWithholding/Gift/Grant/Exercise).
    Drops grant rows (value 0) and empty/parse-fail rows (value 0). Mirrors the
    NETFLOW_EXCLUDE set used by net_flow. Does NOT affect net_flow/breach/cluster —
    those are computed from in_window separately."""
    try:
        val = float(t.get("value_usd") or 0)
    except (TypeError, ValueError):
        val = 0.0
    return val > 0 and t.get("action") not in NETFLOW_EXCLUDE


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

    breaches = [t for _, t in in_window if t.get("breach")]
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
        "window_txns": [t for _, t in in_window if _is_real_insider_txn(t)],
        "all_txns": txns,
        "breaches": breaches,
        "senior_breaches": senior_breaches,
        "cluster": cluster,
        "cluster_dir": cluster_dir,
    }


def render_insider_section(insider_data, vault_root, asof: _date) -> list:
    lines = []
    lines.append("## 내부자 거래 (SEC Form 4)")
    lines.append("")
    lines.append(
        f"최근 SEC Form 4 공시 기준. 순매수액(net $ flow)은 최근 {NETFLOW_WINDOW_DAYS}일 "
        "거래일 기준, 공개시장 Buy − Sell 만 합산 — "
        "TaxWithholding · Gift · Grant · Exercise(기계적 거래)는 제외. "
        "⚠️ = 내부 임계치 초과(breach). 클러스터 = ~5일 내 동일 방향 내부자 ≥3명."
    )
    lines.append("")

    by_ticker = (insider_data or {}).get("by_ticker", {}) or {}

    active = []   # tickers with activity in window
    quiet = []    # tickers with no activity in window
    for tk in TICKERS:
        hist = load_insider_history(vault_root, tk)
        summ = compute_insider_summary(hist, asof) if hist else None
        if summ and summ["window_txns"]:
            active.append((tk, summ))
        else:
            quiet.append(tk)

    if not active:
        lines.append(f"최근 {NETFLOW_WINDOW_DAYS}일 내 윈도우 활동 없음.")
        lines.append("")
        return lines

    # Sort active tickers: most net SELLING (most negative net_flow) first —
    # surfaces distribution pressure; ties broken by abs(net_flow).
    active.sort(key=lambda x: (x[1]["net_flow"], -abs(x[1]["net_flow"])))

    for tk, summ in active:
        lines.append(f"### {tk}")
        lines.append("")
        flags = []
        flags.append(f"순매수액(~{NETFLOW_WINDOW_DAYS}d): {signed_money(summ['net_flow'])}")
        flags.append(f"매수 {money(summ['buy_usd'])} / 매도 {money(summ['sell_usd'])}")
        if summ["cluster"]:
            dir_kr = "매도" if summ["cluster_dir"] == "Sell" else "매수"
            flags.append(f"클러스터 ×{summ['cluster']} ({dir_kr})")
        if summ["senior_breaches"]:
            flags.append(f"⚠️ 고위직 breach {len(summ['senior_breaches'])}건")
        elif summ["breaches"]:
            flags.append(f"⚠️ breach {len(summ['breaches'])}건")
        lines.append(" · ".join(flags))
        lines.append("")

        # Surface senior-role breaches first in the table ordering.
        wtxns = summ["window_txns"]
        wtxns_sorted = sorted(
            wtxns,
            key=lambda t: (
                0 if (t.get("breach") and role_is_senior(t.get("role"))) else
                1 if t.get("breach") else 2,
                t.get("date") or "",
            ),
        )
        lines.append("| 거래일 | 내부자 | 직책 | 유형 | 수량 | 단가 | 거래금액 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for t in wtxns_sorted:
            tdate = t.get("date") or "—"
            name = t.get("insider_name") or "—"
            role = t.get("role") or "—"
            action = t.get("action") or "—"
            shares = comma_int(t.get("shares"))
            price = money(t.get("price_usd"))
            value = money(t.get("value_usd"))
            if t.get("breach"):
                value = f"{value} ⚠️"
                if role_is_senior(role):
                    name = f"**{name}**"
            lines.append(
                f"| {tdate} | {name} | {role} | {action} | {shares} | {price} | {value} |"
            )
        lines.append("")

    if quiet:
        lines.append(f"*활동 없음 (~{NETFLOW_WINDOW_DAYS}d): {', '.join(quiet)}*")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    if len(sys.argv) < 2:
        print("usage: render_m7_tracker.py <vault_root> [<date>]", file=sys.stderr)
        return 1

    vault_root = Path(sys.argv[1]).expanduser().resolve()
    want_date = sys.argv[2] if len(sys.argv) > 2 else _date.today().strftime("%Y-%m-%d")
    asof = _parse_date(want_date) or _date.today()

    short_data = load_category(vault_root, "short", want_date)
    insider_data = load_category(vault_root, "insider", want_date)
    snapshot = load_metrics_snapshot(vault_root)

    short_empty = not short_data or not (short_data.get("by_ticker") or {})
    insider_empty = not insider_data or not (insider_data.get("by_ticker") or {})
    if short_empty and insider_empty:
        print(f"[render_m7_tracker] no M7 data for {want_date} (and no fallback) — skipping write.")
        return 2

    data_date = (
        (short_data or {}).get("date")
        or (insider_data or {}).get("date")
        or want_date
    )

    lines = []
    lines.append("---")
    lines.append('title: "M7 트래커 — 내부자 거래 · 공매도"')
    lines.append("public: true")
    lines.append("type: reference")
    lines.append(f"date: {data_date}")
    lines.append("tags:")
    lines.append("  - ctx/public")
    lines.append("  - stockdog")
    lines.append("  - m7")
    lines.append("  - tracker")
    lines.append("  - region/us")
    lines.append("---")
    lines.append("")
    lines.append("# M7 트래커 — 내부자 거래 · 공매도")
    lines.append("")
    lines.append(
        "> Magnificent 7 (AAPL · AMZN · GOOGL · META · MSFT · NVDA · TSLA)의 "
        "SEC Form 4 내부자 거래와 FINRA RegSHO 공매도 비중을 추적합니다. "
        "이 페이지는 StockDog M7 파이프라인이 매일 자동 갱신(덮어쓰기)합니다. "
        "추세·평균은 누적 데이터 기반이며, 30일 미만 구간은 (~Nd, building)으로 표기합니다."
    )
    lines.append("")
    lines.extend(render_regime_section(snapshot))
    lines.extend(render_short_section(short_data, vault_root, snapshot))
    lines.extend(render_insider_section(insider_data, vault_root, asof))
    lines.append("")
    lines.append(
        '<details class="dash-refs" open>\n'
        '<summary>출처 / References</summary>\n'
        '<div class="dash-refs-body">\n'
        '<p class="dash-refs-lede">데이터 출처 · 자동 생성 — 관찰용이며 매매 시그널이 아닙니다.</p>\n'
        '<a class="dash-ref-chip" href="https://www.sec.gov" target="_blank" rel="noopener">sec.gov</a>\n'
        '<a class="dash-ref-chip" href="https://www.finra.org" target="_blank" rel="noopener">finra.org</a>\n'
        '<a class="dash-ref-chip" href="https://www.cnn.com/markets/fear-and-greed" target="_blank" rel="noopener">cnn.com</a>\n'
        f'<p class="dash-refs-meta">자동 생성 · {data_date}</p>\n'
        '</div>\n'
        '</details>'
    )
    lines.append("")

    out_dir = vault_root / "10_Public" / "trackers"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "m7.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    short_n = sum(
        1 for tk in TICKERS if (short_data or {}).get("by_ticker", {}).get(tk)
    ) if short_data else 0
    print(
        f"[render_m7_tracker] wrote {out_path} "
        f"(data_date={data_date}, short_tickers={short_n}, regime={'yes' if snapshot else 'no'})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
