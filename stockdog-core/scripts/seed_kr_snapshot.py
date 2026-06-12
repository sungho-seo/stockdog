#!/usr/bin/env python3
"""One-off seed for raw/stockdog/kr/kr_snapshot.json — NO external API call.

Parses the latest PUBLISHED KR daily report
  ~/service/skyler/raw/stockdog/daily-market/<latest>/Market_Report_KR_*.md
which already contains KOSPI/KOSDAQ (주요 지수 table), USD/KRW (환율 table),
movers (개별 종목 동향 table) and the 시장 요약 callout. Re-uses the SAME
write contract as the live pipeline (utils.kr_snapshot.write_kr_snapshot:
atomic tmp+replace, mkdir -p, ensure_ascii=False) so the seeded file is
byte-shape-identical to what kr_pipeline.save() would emit.

Usage:
  python3 scripts/seed_kr_snapshot.py            # newest report
  python3 scripts/seed_kr_snapshot.py 2026-06-12 # a specific report date

This is a build-time seed only; the live page is refreshed by kr_pipeline.
"""
import glob
import os
import re
import sys

# Make the package importable when run as a plain script from stockdog-core.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.kr_snapshot import write_kr_snapshot  # noqa: E402

VAULT = os.path.join(os.path.expanduser("~"), "service", "skyler")
DM = os.path.join(VAULT, "raw", "stockdog", "daily-market")
SNAP = os.path.join(VAULT, "raw", "stockdog", "kr", "kr_snapshot.json")

# Index label → snapshot key.
_INDEX_LABELS = {"코스피": "KOSPI", "코스닥": "KOSDAQ"}


def _num(s):
    """'7,763.95' / '+0.43%' / '478,729,575' → float (None on failure)."""
    if s is None:
        return None
    s = s.strip().replace(",", "").replace("%", "").replace("원", "")
    s = s.replace("*", "").strip()
    if s in ("", "N/A", "—", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find_report(date_arg):
    if date_arg:
        cands = glob.glob(os.path.join(DM, date_arg, "Market_Report_KR_*.md"))
        if cands:
            return sorted(cands)[-1]
        sys.exit(f"no KR report found for {date_arg}")
    # newest dated dir with a KR report
    dirs = sorted(d for d in glob.glob(os.path.join(DM, "2*")) if os.path.isdir(d))
    for d in reversed(dirs):
        cands = glob.glob(os.path.join(d, "Market_Report_KR_*.md"))
        if cands:
            return sorted(cands)[-1]
    sys.exit("no KR report found in daily-market/")


def _split_row(line):
    """Split a markdown table row into trimmed cell strings."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _parse(md, report_date):
    indices = {}
    usd_krw = None
    movers = []

    # ---- 주요 지수 table: | 지수 | 종가 | 전일 종가 | 등락률 | 거래량 | 해석 |
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        if len(cells) < 5:
            continue
        label = cells[0]
        # index rows
        if label in _INDEX_LABELS:
            close = _num(cells[1])
            prev_close = _num(cells[2])
            change_pct = _num(cells[3])
            volume = _num(cells[4])
            indices[_INDEX_LABELS[label]] = {
                "close": close,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "volume": int(volume) if volume is not None else None,
                "base_date": report_date,
            }

    # ---- 환율 table: | 통화쌍 | 현재 환율 | 전일 환율 | 등락률 | 시사점 |
    m = re.search(r"##\s*환율(.+?)(?:\n##|\Z)", md, re.S)
    if m:
        for line in m.group(1).splitlines():
            cells = _split_row(line) if line.strip().startswith("|") else []
            if len(cells) >= 4 and "USD" in cells[0].upper():
                usd_krw = {"rate": _num(cells[1]), "change_pct": _num(cells[3])}
                break

    # ---- 개별 종목 동향: | 종목명 | 종가 | 전일 종가 | 등락률 | 거래량 | 시장 | 분석 |
    m = re.search(r"##\s*개별 종목 동향(.+?)(?:\n##|\Z)", md, re.S)
    if m:
        for line in m.group(1).splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = _split_row(line)
            if len(cells) < 6:
                continue
            name = cells[0]
            if name in ("종목명", "") or set(name) <= set("-: "):
                continue   # header / separator
            close = _num(cells[1])
            if close is None:
                continue
            movers.append({
                "name": name,
                "code": None,   # report has no code column; live pipeline fills it
                "close": int(close),
                "prev_close": int(_num(cells[2])) if _num(cells[2]) is not None else None,
                "change_pct": _num(cells[3]),
                "volume": int(_num(cells[4])) if _num(cells[4]) is not None else None,
                "market": cells[5] or None,
            })
    movers.sort(key=lambda x: abs(x.get("change_pct") or 0), reverse=True)

    # ---- hero one-liner from indices (deterministic) ----
    parts = []
    for key, label in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
        cp = (indices.get(key) or {}).get("change_pct")
        if cp is None:
            continue
        sign = "+" if cp > 0 else ""
        parts.append(f"{label} {sign}{cp:.2f}%")
    hero = "·".join(parts) if parts else None

    # ---- story: the 시장 요약 callout body (2-3문단 source) ----
    story = None
    m = re.search(r">\s*\[!summary\][^\n]*\n((?:>.*\n?)+)", md)
    if m:
        lines = [re.sub(r"^>\s?", "", ln) for ln in m.group(1).splitlines()]
        story = " ".join(s.strip() for s in lines if s.strip()) or None

    return indices, usd_krw, movers, hero, story


def main():
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    path = _find_report(date_arg)
    # report date from the parent dir name (YYYY-MM-DD) = 발간일/updated.
    report_date = os.path.basename(os.path.dirname(path))
    with open(path, encoding="utf-8") as f:
        md = f.read()

    # data_date: prefer the explicit `data_as_of:` frontmatter, else fall back
    # to the report dir date.
    m = re.search(r"^data_as_of:\s*(\d{4}-\d{2}-\d{2})", md, re.M)
    data_date = m.group(1) if m else report_date

    indices, usd_krw, movers, hero, story = _parse(md, data_date)

    snapshot = {
        "updated": report_date,
        "data_date": data_date,
        "indices": indices,
        "usd_krw": usd_krw,
        "movers": movers,
        "narrative": ({"hero": hero, "story": story}
                      if (hero or story) else None),
        "report_slug": f"/daily-reports/{report_date}-kr",
        "investor_flows": None,   # P2 (수급) — null in P1
    }

    out = write_kr_snapshot(SNAP, snapshot)
    print(f"[seed_kr_snapshot] source: {path}")
    print(f"[seed_kr_snapshot] wrote:  {out}")
    print(f"[seed_kr_snapshot] indices={list(indices)} "
          f"movers={len(movers)} usd_krw={'y' if usd_krw else 'n'}")


if __name__ == "__main__":
    main()
