#!/usr/bin/env python3
"""Render the watchlist public markdown tracker (IMPR-062) — price · volume.

Host-side, stdlib only (json, sys, statistics, datetime, pathlib). Reads the
staged watchlist snapshot and writes a single public tracker page:
    <vault_root>/10_Public/trackers/watchlist.md

raw/ is READ-ONLY — this script only reads raw/stockdog/watchlist/watchlist_snapshot.json
and writes under 10_Public/. The snapshot is staged container-side by
stage_watchlist_snapshot() (the renderer never reads per-ticker history directly).

Cadence note: price/volume are DAILY (US trading days). Leverage/inverse ETFs
carry daily-rebalance decay over multi-day holds — flagged ⚠decay (observational).

Usage:
    render_watchlist_tracker.py <vault_root> [<date>]   # date default = today
"""

import json
import sys
from datetime import date as _date, datetime
from pathlib import Path
from statistics import mean

# ===========================================================================
# copied from render_m7_tracker.py — keep in sync
# (C2: helpers duplicated intentionally so render_m7_tracker.py /
#  render_macro_tracker.py stay byte-for-byte unchanged. If you fix a bug in a
#  helper here, mirror it there and vice-versa — do NOT import from them.)
# ===========================================================================
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _is_num(v) -> bool:
    """True for a real finite number (excludes None / NaN / non-numeric)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f  # False for NaN


def comma_int(value) -> str:
    if not _is_num(value):
        return "—"
    n = int(round(float(value)))
    return f"{n:,}"


def sparkline(values) -> str:
    """Unicode sparkline over a numeric series (already in display order)."""
    nums = [float(v) for v in values if _is_num(v)]
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


def fmt(value, dp=2) -> str:
    """Fixed-decimal number, '—' on None/NaN/non-numeric."""
    if not _is_num(value):
        return "—"
    return f"{float(value):.{dp}f}"


# ===========================================================================
# static category layout (planner spec)
# ===========================================================================
CATEGORIES = [
    ("지수 ETF", ["SPY", "QQQ"]),
    ("개별 종목", ["TSLA", "ANET", "IONQ"]),
    ("레버리지/인버스", ["ETHU", "METU", "NVDL", "TSLL", "TSLT", "ANEL", "FNGU", "BULZ"]),
]
LEVERAGED = set(CATEGORIES[2][1])

BUILDING_THRESHOLD = 30   # < this many points → metric "—" / "(~Nd, building)" label
VOL_WINDOW = 20           # trailing window for volume-vs-average
MEAN_WINDOW = 60          # trailing window for price 평균대비
VOL_SPIKE_MULT = 2.0      # volume ratio ≥ this → 거래량▲ flag
PROX = 0.02               # within 2% of window high/low → 근접 flag
D5_LOOKBACK = 5           # Δ5d lookback (closes[-1] vs closes[-1-5])


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------
def load_watchlist_snapshot(vault_root: Path):
    path = vault_root / "raw" / "stockdog" / "watchlist" / "watchlist_snapshot.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _pct(numer, denom):
    if numer is None or denom in (None, 0):
        return None
    return numer / denom * 100.0


# ---------------------------------------------------------------------------
# per-ticker row computation
# ---------------------------------------------------------------------------
def compute_row(tk, info):
    """Compute the display cells for one ticker. Missing/empty → all '—'.

    Returns a dict of pre-formatted cells plus `n` (history length).
    """
    blank = {
        "tk": tk, "name": "—", "price": "—", "d1d": "—", "d5d": "—",
        "spark": "—", "vs_mean": "—", "volume": "—", "vol_ratio": "—",
        "note": "—", "n": 0,
    }
    if not info:
        return blank

    name = info.get("name") or "—"
    history = info.get("history") or []
    latest = info.get("latest") or {}

    closes = [float(h.get("close")) for h in history if _is_num(h.get("close"))]
    vols = [float(h.get("volume")) for h in history if _is_num(h.get("volume"))]
    n = len(closes)
    if n == 0:
        b = dict(blank)
        b["name"] = name
        return b

    # 가격 = latest.close (fall back to last history close)
    price_v = float(latest["close"]) if _is_num(latest.get("close")) else None
    if price_v is None:
        price_v = closes[-1]

    # Δ1d = latest.change_pct
    d1d_v = float(latest["change_pct"]) if _is_num(latest.get("change_pct")) else None
    if d1d_v is None and history and _is_num(history[-1].get("change_pct")):
        d1d_v = float(history[-1]["change_pct"])

    # Δ5d = (closes[-1] - closes[-1-5]) / closes[-1-5] * 100, needs ≥6 points
    if n >= D5_LOOKBACK + 1 and closes[-1 - D5_LOOKBACK] not in (None, 0):
        d5d_v = (closes[-1] - closes[-1 - D5_LOOKBACK]) / closes[-1 - D5_LOOKBACK] * 100.0
    else:
        d5d_v = None

    # 추세 sparkline over all closes
    spark = sparkline(closes)

    # 평균대비 vs trailing MEAN_WINDOW close mean — only when N ≥ BUILDING_THRESHOLD
    if n >= BUILDING_THRESHOLD:
        window = closes[-MEAN_WINDOW:]
        m = mean(window) if window else None
        vs_mean_v = _pct(closes[-1] - m, m) if m not in (None, 0) else None
    else:
        vs_mean_v = None

    # 거래량 = latest.volume
    vol_v = float(latest["volume"]) if _is_num(latest.get("volume")) else None
    if vol_v is None and vols:
        vol_v = vols[-1]

    # 거래량 vs 평균: latest vol / mean(vols[-VOL_WINDOW:]); need ≥5 vol points
    vol_ratio_v = None
    if len(vols) >= 5 and vol_v is not None:
        vw = vols[-VOL_WINDOW:]
        vm = mean(vw) if vw else None
        if vm not in (None, 0):
            vol_ratio_v = vol_v / vm

    # 비고 flags
    flags = []
    if vol_ratio_v is not None and vol_ratio_v >= VOL_SPIKE_MULT:
        flags.append("거래량▲")
    if n >= BUILDING_THRESHOLD and price_v is not None:
        hi = max(closes)
        lo = min(closes)
        if hi not in (None, 0) and abs(price_v - hi) / hi <= PROX:
            flags.append(f"{n}일 고점 근접")
        elif lo not in (None, 0) and abs(price_v - lo) / lo <= PROX:
            flags.append(f"{n}일 저점 근접")
    if tk in LEVERAGED:
        flags.append("⚠decay")

    # formatting
    def signed_pct(v, dp=2):
        if v is None:
            return "—"
        sign = "+" if v >= 0 else "−"
        return f"{sign}{abs(v):.{dp}f}%"

    return {
        "tk": tk,
        "name": name,
        "price": fmt(price_v, 2),
        "d1d": signed_pct(d1d_v),
        "d5d": signed_pct(d5d_v),
        "spark": f"`{spark}`" if spark else "—",
        "vs_mean": signed_pct(vs_mean_v),
        "volume": comma_int(vol_v),
        "vol_ratio": (f"{vol_ratio_v:.1f}×" if vol_ratio_v is not None else "—"),
        "note": " · ".join(flags) if flags else "—",
        "n": n,
    }


def _building_label(n):
    return f" (~{n}d, building)" if n < BUILDING_THRESHOLD else ""


def render_group(title, tickers, snap_tickers) -> list:
    """One ### group section: header + table."""
    lines = []
    rows = [compute_row(tk, snap_tickers.get(tk)) for tk in tickers]
    # max history depth in group for the (~Nd) column labels / building note
    n_max = max((r["n"] for r in rows), default=0)
    nd = n_max if n_max else 0

    lines.append(f"### {title}")
    lines.append("")
    lines.append(
        f"| 종목 | 이름 | 가격 | Δ1d | Δ5d | 추세 (~{nd}d) | 평균대비 (~{nd}d) "
        "| 거래량 | 거래량 vs 평균 | 비고 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        lines.append(
            f"| {r['tk']} | {r['name']} | {r['price']} | {r['d1d']} | {r['d5d']} "
            f"| {r['spark']} | {r['vs_mean']} | {r['volume']} | {r['vol_ratio']} | {r['note']} |"
        )
    lines.append("")
    # honesty label if any ticker in this group is under threshold
    if any(0 < r["n"] < BUILDING_THRESHOLD for r in rows):
        lines.append(f"*일부 종목은 30일 미만 — 평균대비는 누적 후 표시{_building_label(nd)}.*")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    if len(sys.argv) < 2:
        print("usage: render_watchlist_tracker.py <vault_root> [<date>]", file=sys.stderr)
        return 1

    vault_root = Path(sys.argv[1]).expanduser().resolve()
    want_date = sys.argv[2] if len(sys.argv) > 2 else _date.today().strftime("%Y-%m-%d")

    snapshot = load_watchlist_snapshot(vault_root)
    if not snapshot:
        print("[render_watchlist_tracker] no watchlist snapshot found — skipping write.")
        return 2

    snap_tickers = snapshot.get("tickers") or {}
    updated = snapshot.get("updated", want_date)

    # Any snapshot ticker not in the static layout → trailing "기타" group.
    known = {tk for _, tks in CATEGORIES for tk in tks}
    extra = [tk for tk in snap_tickers if tk not in known]

    lines = []
    lines.append("---")
    lines.append('title: "워치리스트 트래커 — 가격·거래량"')
    lines.append("public: true")
    lines.append("type: reference")
    lines.append(f"date: {updated}")
    lines.append("tags:")
    lines.append("  - ctx/public")
    lines.append("  - stockdog")
    lines.append("  - watchlist")
    lines.append("  - tracker")
    lines.append("  - region/us")
    lines.append("---")
    lines.append("")
    lines.append("# 워치리스트 트래커 — 가격·거래량")
    lines.append("")
    lines.append(
        "> 13개 US 종목·ETF의 가격·거래량을 일간 자동 추적합니다. "
        "레버리지/인버스 ETF는 일일 리밸런싱 decay로 다일 보유 시 기초자산과 괴리가 누적될 수 있어 ⚠decay로 표기합니다. "
        "관찰용 트래커이며 매매 시그널이 아닙니다."
    )
    lines.append(
        "> 심리·금리 컨텍스트는 [[trackers/m7|M7]] · [[trackers/macro|매크로]] 트래커 참조."
    )
    lines.append("")

    for title, tickers in CATEGORIES:
        lines.extend(render_group(title, tickers, snap_tickers))

    if extra:
        lines.extend(render_group("기타", extra, snap_tickers))

    lines.append("")
    lines.append(
        '<details class="dash-refs" open>\n'
        '<summary>출처 / References</summary>\n'
        '<div class="dash-refs-body">\n'
        '<p class="dash-refs-lede">데이터 출처 · 자동 생성 — 관찰용이며 매매 시그널이 아닙니다.</p>\n'
        '<a class="dash-ref-chip" href="https://finance.yahoo.com" target="_blank" rel="noopener">finance.yahoo.com</a>\n'
        f'<p class="dash-refs-meta">자동 생성 · {updated}</p>\n'
        '</div>\n'
        '</details>'
    )
    lines.append("")

    out_dir = vault_root / "10_Public" / "trackers"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "watchlist.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(
        f"[render_watchlist_tracker] wrote {out_path} "
        f"(tickers={len(snap_tickers)}, updated={updated})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
