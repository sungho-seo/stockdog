#!/usr/bin/env python3
"""Render the M7 (internal trades + short interest) public markdown tracker.

IMPR-058 Step 3 — stockdog side. Stdlib only (json, pathlib, sys, datetime).
Reads the aggregate dated M7 JSON files and writes a single public tracker page:
    <vault_root>/10_Public/trackers/m7.md

Usage:
    render_m7_tracker.py <vault_root> [<date>]   # date default = today (%Y-%m-%d)

raw/ is READ-ONLY — this script only reads from it and writes under 10_Public/.
"""

import json
import sys
from datetime import date as _date
from pathlib import Path

# Canonical ticker order — deterministic, NOT dict insertion order.
TICKERS = ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]


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


def comma_int(value) -> str:
    """Thousands-comma integer, rounding fractional values. '—' on bad/zero input."""
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


def ratio_pct(value) -> str:
    """short_ratio -> '{:.1f}%'. '—' on bad input."""
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "—"


def render_short_section(short_data) -> list:
    lines = []
    lines.append("## 공매도 비중 (FINRA RegSHO)")
    lines.append("")

    by_ticker = (short_data or {}).get("by_ticker", {}) or {}
    file_used = (short_data or {}).get("file_used", "—")
    freshness = (short_data or {}).get("freshness", "—")
    lines.append(f"데이터 기준일 · 신선도: {file_used} · {freshness}")
    lines.append("")
    lines.append("| 티커 | 공매도 비중 | 공매도량 | 총거래량 | 기준일 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for tk in TICKERS:
        row = by_ticker.get(tk) or {}
        if row.get("error"):
            lines.append(f"| {tk} | — | — | — | — |")
            continue
        ratio = ratio_pct(row.get("short_ratio"))
        svol = comma_int(row.get("short_volume"))
        tvol = comma_int(row.get("total_volume"))
        as_of = row.get("data_as_of") or "—"
        lines.append(f"| {tk} | {ratio} | {svol} | {tvol} | {as_of} |")
    lines.append("")
    return lines


def render_insider_section(insider_data) -> list:
    lines = []
    lines.append("## 내부자 거래 (SEC Form 4)")
    lines.append("")
    lines.append("최근 SEC Form 4 공시 기준. ⚠️ 표시는 내부 임계치 초과(breach) 거래.")
    lines.append("")

    by_ticker = (insider_data or {}).get("by_ticker", {}) or {}
    for tk in TICKERS:
        lines.append(f"### {tk}")
        lines.append("")
        row = by_ticker.get(tk) or {}
        txns = row.get("transactions") or []
        if not txns:
            lines.append("최근 내부자 거래 없음")
            lines.append("")
            continue
        lines.append("| 거래일 | 내부자 | 직책 | 유형 | 수량 | 단가 | 거래금액 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for t in txns:
            tdate = t.get("date") or "—"
            name = t.get("insider_name") or "—"
            role = t.get("role") or "—"
            action = t.get("action") or "—"
            shares = comma_int(t.get("shares"))
            price = money(t.get("price_usd"))
            value = money(t.get("value_usd"))
            if t.get("breach"):
                value = f"{value} ⚠️"
            lines.append(
                f"| {tdate} | {name} | {role} | {action} | {shares} | {price} | {value} |"
            )
        lines.append("")
    return lines


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: render_m7_tracker.py <vault_root> [<date>]", file=sys.stderr)
        return 1

    vault_root = Path(sys.argv[1]).expanduser().resolve()
    want_date = sys.argv[2] if len(sys.argv) > 2 else _date.today().strftime("%Y-%m-%d")

    short_data = load_category(vault_root, "short", want_date)
    insider_data = load_category(vault_root, "insider", want_date)

    short_empty = not short_data or not (short_data.get("by_ticker") or {})
    insider_empty = not insider_data or not (insider_data.get("by_ticker") or {})
    if short_empty and insider_empty:
        print(f"[render_m7_tracker] no M7 data for {want_date} (and no fallback) — skipping write.")
        return 2

    # DATA_DATE = the JSON's own `date` field, so a fallback-to-older file stays
    # self-consistent. Prefer short, then insider, then the requested date.
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
        "이 페이지는 StockDog M7 파이프라인이 매일 자동 갱신(덮어쓰기)합니다."
    )
    lines.append("")
    lines.extend(render_short_section(short_data))
    lines.extend(render_insider_section(insider_data))
    lines.append(
        f"*데이터 출처: SEC EDGAR (Form 4), FINRA RegSHO. 자동 생성 — "
        f"StockDog M7 파이프라인. 마지막 갱신: {data_date}.*"
    )
    lines.append("")

    out_dir = vault_root / "10_Public" / "trackers"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "m7.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    short_n = sum(
        1 for tk in TICKERS if (short_data or {}).get("by_ticker", {}).get(tk)
    ) if short_data else 0
    insider_n = sum(
        1
        for tk in TICKERS
        if ((insider_data or {}).get("by_ticker", {}).get(tk) or {}).get("transactions")
    ) if insider_data else 0
    print(
        f"[render_m7_tracker] wrote {out_path} "
        f"(data_date={data_date}, short_tickers={short_n}, insider_active={insider_n})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
