#!/usr/bin/env python3
"""Phase C free regeneration: inject the 업종(sector) rotation block into the
EXISTING kr_snapshot.json — NO LLM, NO --sample, NO full pipeline re-run.

Reads the live snapshot, fetches the full KR 업종 list (signed 등락률 + 상승/하락
종목수) from the free Naver mobile industry endpoint
(collectors.kr_sectors.fetch_kr_sectors — same free host kr_breadth already
uses), wraps it as a `sectors` block carrying the snapshot's data_date, and
rewrites the snapshot atomically (same write contract as the live pipeline).

This mirrors scripts/enrich_kr_flow_insights.py — it does NOT call main.py /
analyze() (which would hit the paid Sonnet API). FREE path only.

Usage (inside the container):
    python scripts/enrich_kr_sectors.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.kr_sectors import fetch_kr_sectors  # noqa: E402
from utils.kr_snapshot import write_kr_snapshot  # noqa: E402

# /notes is the in-container mount of ~/service/skyler (see docker-compose.yml).
SNAP = "/notes/raw/stockdog/kr/kr_snapshot.json"


def main():
    if not os.path.exists(SNAP):
        sys.exit(f"[enrich_kr_sectors] snapshot not found: {SNAP}")
    with open(SNAP, encoding="utf-8") as f:
        snap = json.load(f)

    sectors = fetch_kr_sectors()
    if not isinstance(sectors, list) or not sectors:
        sys.exit("[enrich_kr_sectors] no sectors fetched (Naver miss?) — aborting")

    snap["sectors"] = {
        "data_date": snap.get("data_date"),
        "items": sectors,
    }

    out = write_kr_snapshot(SNAP, snap)
    print(f"[enrich_kr_sectors] wrote: {out}")
    print(f"[enrich_kr_sectors] sectors: {len(sectors)} 업종 "
          f"(data_date={snap.get('data_date')})")
    # Echo a SIGN-CORRECTNESS proof: count neg/pos + show top/bottom 3.
    neg = sum(1 for s in sectors if (s.get("change_pct") or 0) < 0)
    pos = sum(1 for s in sectors if (s.get("change_pct") or 0) > 0)
    print(f"[enrich_kr_sectors] sign check: {neg} 하락 / {pos} 상승 "
          f"(down day → 하락 should dominate)")
    print(f"[enrich_kr_sectors] TOP3: "
          f"{[(s['name'], s['change_pct']) for s in sectors[:3]]}")
    print(f"[enrich_kr_sectors] BOTTOM3: "
          f"{[(s['name'], s['change_pct']) for s in sectors[-3:]]}")


if __name__ == "__main__":
    main()
