#!/usr/bin/env python3
"""Render the macro public markdown tracker (IMPR-061) — rates · inflation · policy · dollar.

Host-side, stdlib only (json, sys, datetime, pathlib). Reads the staged macro
snapshot and writes a single public tracker page:
    <vault_root>/10_Public/trackers/macro.md

raw/ is READ-ONLY — this script only reads raw/stockdog/macro/macro_snapshot.json
and writes under 10_Public/. It does NOT read metrics_history.db (root-owned,
gitignored); the snapshot is staged container-side by stage_macro_snapshot().

Cadence note: yields / spread / fed funds / broad dollar / USD-KRW are DAILY;
CPI / Core CPI / PPI are MONTHLY (YoY); FOMC meets ~8×/yr.

Usage:
    render_macro_tracker.py <vault_root> [<date>]   # date default = today
"""

import json
import sys
from datetime import date as _date, datetime
from pathlib import Path

# ===========================================================================
# copied from render_m7_tracker.py — keep in sync
# (M4: helpers duplicated intentionally so render_m7_tracker.py stays byte-for-byte
#  unchanged. If you fix a bug in a helper here, mirror it there and vice-versa.)
# ===========================================================================
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def comma_int(value) -> str:
    if value is None:
        return "—"
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return "—"
    return f"{n:,}"


def sparkline(values) -> str:
    """Unicode sparkline over a numeric series (already in display order)."""
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
    """CNN-style Fear & Greed zone label. (copied; unused here but kept in sync)"""
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


# ===========================================================================
# host-side FOMC schedule (copied from collectors/economic_calendar.py — keep in sync)
# 2027 unpublished as of build → empty list triggers M6 graceful fallback.
# ===========================================================================
FOMC_DATES = [
    "2026-01-28", "2026-03-18", "2026-05-06",
    "2026-06-17", "2026-07-29", "2026-09-16",
    "2026-10-28", "2026-12-16",
    # 2027: TODO confirm from federalreserve.gov
]

BUILDING_THRESHOLD = 30  # < this many points → "(~Nd, building)" honesty label


# ---------------------------------------------------------------------------
# loaders / small helpers
# ---------------------------------------------------------------------------
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


def _building_label(n):
    return f" (~{n}d, building)" if n < BUILDING_THRESHOLD else ""


def fmt_pct(v, dp=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{dp}f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_num(v, dp=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{dp}f}"
    except (TypeError, ValueError):
        return "—"


def signed_bps(latest, nback):
    """(latest − nback) in basis points, signed, e.g. '+12bps' / '−5bps' / '—'."""
    if latest is None or nback is None:
        return "—"
    bps = (latest - nback) * 100
    sign = "+" if bps >= 0 else "−"
    return f"{sign}{abs(bps):.0f}bps"


def signed_point(latest, nback, dp=2):
    if latest is None or nback is None:
        return "—"
    d = latest - nback
    sign = "+" if d >= 0 else "−"
    return f"{sign}{abs(d):.{dp}f}"


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
def render_context(daily, n) -> list:
    """## 시장 컨텍스트 — spread headline, US 10Y, broad dollar."""
    t10y2y = _col(daily, "t10y2y")
    y10 = _col(daily, "macro_10y")
    dxy = _col(daily, "dxy_broad")

    lines = ["## 시장 컨텍스트", ""]
    spread_latest = _last_valid(t10y2y)
    spread_5d = _nth_back_valid(t10y2y, 5)
    y10_latest = _last_valid(y10)
    y10_5d = _nth_back_valid(y10, 5)
    dxy_latest = _last_valid(dxy)
    dxy_5d = _nth_back_valid(dxy, 5)

    inv = " · ⚠️ 역전" if (spread_latest is not None and spread_latest < 0) else ""
    lines.append("| 지표 | 최신 | 추세 | Δ5d |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(
        f"| 10Y-2Y 스프레드{inv} | {fmt_pct(spread_latest)} | `{sparkline(t10y2y)}` "
        f"| {signed_bps(spread_latest, spread_5d)} |"
    )
    lines.append(
        f"| US 10Y | {fmt_pct(y10_latest, 3)} | `{sparkline(y10)}` "
        f"| {signed_bps(y10_latest, y10_5d)} |"
    )
    lines.append(
        f"| 광의 달러 (브로드) | {fmt_num(dxy_latest, 3)} | `{sparkline(dxy)}` "
        f"| {signed_point(dxy_latest, dxy_5d, 3)} |"
    )
    lines.append("")
    lines.append(f"*금리·환율은 일간 갱신{_building_label(n)}.*")
    lines.append("")
    return lines


def render_curve(daily, n, freshness) -> list:
    """## 금리 곡선 (UST) — 2Y / 10Y / 30Y / spread rows."""
    lines = ["## 금리 곡선 (UST)", ""]
    lines.append(f"기준일: {freshness or '—'}{_building_label(n)}")
    lines.append("")
    lines.append("| 만기 | 금리 | Δ5d | Δ20d | 추세 | 추이 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")

    rows = [
        ("2Y",        "us_2y",     3),
        ("10Y",       "macro_10y", 3),
        ("30Y",       "us_30y",    3),
        ("10Y-2Y",    "t10y2y",    2),
    ]
    for label, key, dp in rows:
        vals = _col(daily, key)
        latest = _last_valid(vals)
        d5 = _nth_back_valid(vals, 5)
        d20 = _nth_back_valid(vals, 20)
        ar = arrow(None if (latest is None or d5 is None) else latest - d5)
        suffix = ""
        if key == "t10y2y" and latest is not None and latest < 0:
            suffix = " ⚠️ 역전"
        lines.append(
            f"| {label}{suffix} | {fmt_pct(latest, dp)} | {signed_bps(latest, d5)} "
            f"| {signed_bps(latest, d20)} | `{sparkline(vals)}` | {ar} |"
        )
    lines.append("")
    lines.append("*Δ는 베이시스포인트(bps). 10Y-2Y < 0 이면 장단기 금리 역전 — 침체 신호로 흔히 관찰됨 (시그널 아님).*")
    lines.append("")
    return lines


def _infl_row(label, series_obj, real10y=None):
    """One inflation table row from an inflation series object."""
    if not series_obj:
        return f"| {label} | — | — | — |"
    latest = series_obj.get("latest") or {}
    history = series_obj.get("history") or []
    yoy = latest.get("yoy")
    base_month = latest.get("date", "—")
    yoy_series = [h.get("yoy") for h in history]
    prev_yoy = yoy_series[-2] if len(yoy_series) >= 2 else None
    spark = sparkline(yoy_series)
    cur_cell = fmt_pct(yoy) if yoy is not None else "—"
    prev_cell = fmt_pct(prev_yoy) if prev_yoy is not None else "—"
    spark_cell = f"`{spark}`" if spark else "—"
    return f"| {label} ({base_month}) | {cur_cell} | {prev_cell} | {spark_cell} |"


def render_inflation(inflation, daily) -> list:
    """## 인플레이션 — CPI / Core CPI / PPI YoY + 실질 10Y."""
    lines = ["## 인플레이션", ""]
    lines.append("월간 지표 — 전년 동월 대비(YoY). 기준월은 라벨에 표기.")
    lines.append("")
    lines.append("| 지표 (기준월) | YoY | 전월 YoY | 추세 (YoY) |")
    lines.append("| --- | --- | --- | --- |")

    inflation = inflation or {}
    cpi = inflation.get("cpi")
    core = inflation.get("core_cpi")
    ppi = inflation.get("ppi")
    lines.append(_infl_row("CPI", cpi))
    lines.append(_infl_row("Core CPI", core))
    lines.append(_infl_row("PPI", ppi))

    # 실질 10Y = US 10Y − Core CPI YoY
    y10_latest = _last_valid(_col(daily, "macro_10y"))
    core_yoy = ((core or {}).get("latest") or {}).get("yoy")
    if y10_latest is not None and core_yoy is not None:
        real10y = round(y10_latest - core_yoy, 2)
        real_cell = fmt_pct(real10y)
    else:
        real_cell = "—"
    lines.append(f"| 실질 10Y (10Y − Core CPI) | {real_cell} | — | — |")
    lines.append("")
    lines.append("*실질 10Y = US 10Y 명목금리 − Core CPI YoY. 13개월 미만 데이터면 YoY는 — 로 표기.*")
    lines.append("")
    return lines


def render_policy(daily, asof: _date) -> list:
    """## 정책 (Fed) — Fed Funds level + FOMC countdown + blackout (widget)."""
    lines = ["## 정책 (Fed)", ""]
    ff_latest = _last_valid(_col(daily, "fed_funds"))
    lines.append(f"- **실효 연방기금금리(EFFR)**: {fmt_pct(ff_latest, 2)}")

    # next FOMC ≥ run date
    upcoming = [d for d in FOMC_DATES if d >= asof.isoformat()]
    if not upcoming:
        lines.append("- **다음 FOMC**: 일정 미공개 (2027 일정 발표 전)")
    else:
        nxt = upcoming[0]
        nxt_date = datetime.strptime(nxt, "%Y-%m-%d").date()
        d_n = (nxt_date - asof).days
        lines.append(f"- **다음 FOMC**: {nxt} (D-{d_n})")
        # blackout: run date within [meeting−10d, meeting+1d]
        from datetime import timedelta
        if (nxt_date - timedelta(days=10)) <= asof <= (nxt_date + timedelta(days=1)):
            lines.append("- **블랙아웃 기간** ⚠️ — Fed 인사 발언 자제 기간 (회의 ±)")
    lines.append("")
    lines.append("*직전 결정·점도표는 추후 추가 예정.*")
    lines.append("")
    return lines


def render_fx(daily, n) -> list:
    """## 환율 / 달러 — broad dollar + USD/KRW."""
    lines = ["## 환율 / 달러", ""]
    dxy = _col(daily, "dxy_broad")
    krw = _col(daily, "usd_krw")
    dxy_latest, dxy_5d = _last_valid(dxy), _nth_back_valid(dxy, 5)
    krw_latest, krw_5d = _last_valid(krw), _nth_back_valid(krw, 5)

    lines.append("| 지표 | 최신 | Δ5d | 추세 | 추이 |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append(
        f"| 광의 달러(브로드, DXY 아님) | {fmt_num(dxy_latest, 3)} "
        f"| {signed_point(dxy_latest, dxy_5d, 3)} | `{sparkline(dxy)}` "
        f"| {arrow(None if (dxy_latest is None or dxy_5d is None) else dxy_latest - dxy_5d)} |"
    )
    lines.append(
        f"| USD/KRW | {fmt_num(krw_latest, 2)} "
        f"| {signed_point(krw_latest, krw_5d, 2)} | `{sparkline(krw)}` "
        f"| {arrow(None if (krw_latest is None or krw_5d is None) else krw_latest - krw_5d)} |"
    )
    lines.append("")
    lines.append(
        "*광의 달러는 FRED DTWEXBGS(무역가중 광의 명목 달러지수)로, ICE DXY와 다릅니다."
        f"{_building_label(n)}*"
    )
    lines.append("")
    return lines


def render_sentiment_ref() -> list:
    """## 심리 컨텍스트 (참조) — cross-link to M7, no duplicate render."""
    return [
        "## 심리 컨텍스트 (참조)",
        "",
        "- VIX · Fear & Greed 지표는 [[trackers/m7|M7 트래커]]에서 확인하세요 (중복 표시 안 함).",
        "- 금리 상승 국면은 일반적으로 장기 성장주(고듀레이션) 밸류에이션에 역풍으로 관찰됩니다.",
        "",
    ]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: render_macro_tracker.py <vault_root> [<date>]", file=sys.stderr)
        return 1

    vault_root = Path(sys.argv[1]).expanduser().resolve()
    want_date = sys.argv[2] if len(sys.argv) > 2 else _date.today().strftime("%Y-%m-%d")
    asof = _parse_date(want_date) or _date.today()

    snapshot = load_macro_snapshot(vault_root)
    if not snapshot:
        print(f"[render_macro_tracker] no macro snapshot found — skipping write.")
        return 2

    daily = snapshot.get("daily") or []
    inflation = snapshot.get("inflation") or {}
    updated = snapshot.get("updated", want_date)
    n = len(daily)
    freshness = daily[-1].get("date") if daily else None

    lines = []
    lines.append("---")
    lines.append('title: "매크로 트래커 — 금리·인플레이션·정책·달러"')
    lines.append("public: true")
    lines.append("type: reference")
    lines.append(f"date: {updated}")
    lines.append("tags:")
    lines.append("  - ctx/public")
    lines.append("  - stockdog")
    lines.append("  - macro")
    lines.append("  - tracker")
    lines.append("  - region/us")
    lines.append("---")
    lines.append("")
    lines.append("# 매크로 트래커 — 금리·인플레이션·정책·달러")
    lines.append("")
    lines.append(
        "> 미국 국채 금리·장단기 스프레드, 인플레이션(CPI/PPI), Fed 정책, 달러를 추적합니다. "
        "금리·환율은 **일간**, CPI·PPI는 **월간(YoY)**, FOMC는 연 8회로 갱신 주기가 섞여 있습니다. "
        "StockDog US 파이프라인이 매일 자동 갱신(덮어쓰기)하며, 30일 미만 구간은 (~Nd, building)으로 표기합니다. "
        "관찰용 트래커이며 매매 시그널이 아닙니다."
    )
    lines.append("")
    lines.extend(render_context(daily, n))
    lines.extend(render_curve(daily, n, freshness))
    lines.extend(render_inflation(inflation, daily))
    lines.extend(render_policy(daily, asof))
    lines.extend(render_fx(daily, n))
    lines.extend(render_sentiment_ref())
    lines.append(
        f"*데이터 출처: FRED (UST 금리·스프레드·EFFR·CPI·PPI·광의 달러), yfinance (USD/KRW). "
        f"자동 생성 — StockDog US 파이프라인. 마지막 갱신: {updated}.*"
    )
    lines.append("")

    out_dir = vault_root / "10_Public" / "trackers"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "macro.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(
        f"[render_macro_tracker] wrote {out_path} "
        f"(daily_rows={n}, freshness={freshness}, updated={updated})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
