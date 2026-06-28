#!/usr/bin/env python3
"""P1-1 free regeneration: inject market-level 수급 INSIGHTS (streak + cum5 +
cum20 per investor per market) into the EXISTING kr_snapshot.json — NO LLM, NO
--sample, NO full pipeline re-run.

Reads the live snapshot, computes the insight block from the free Naver desktop
daily investor-trend backfill (collectors.kr_investor_flow.fetch_market_flow_
insights — same free host already in use), attaches it under
``investor_flows.insights`` and rewrites the snapshot atomically (same write
contract as the live pipeline). The single-day ``investor_flows.market`` block
is left untouched (consistent with the published page).

This mirrors the surgical free-path regeneration used for prior KR fixes
(scripts/repopulate_kr_nxt_prices.py) — it does NOT call main.py / analyze()
(which would hit the paid Sonnet API).

Usage (inside the container):
    python scripts/enrich_kr_flow_insights.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.kr_investor_flow import fetch_market_flow_insights  # noqa: E402
from utils.kr_snapshot import write_kr_snapshot  # noqa: E402

# /notes is the in-container mount of ~/service/skyler (see docker-compose.yml).
SNAP = "/notes/raw/stockdog/kr/kr_snapshot.json"


def main():
    if not os.path.exists(SNAP):
        sys.exit(f"[enrich_kr_flow_insights] snapshot not found: {SNAP}")
    with open(SNAP, encoding="utf-8") as f:
        snap = json.load(f)

    flows = snap.get("investor_flows")
    if not isinstance(flows, dict):
        sys.exit("[enrich_kr_flow_insights] investor_flows absent — nothing to enrich")

    insights = fetch_market_flow_insights()
    if not isinstance(insights, dict):
        sys.exit("[enrich_kr_flow_insights] no insights fetched (Naver miss?) — aborting")

    flows["insights"] = insights
    snap["investor_flows"] = flows

    out = write_kr_snapshot(SNAP, snap)
    print(f"[enrich_kr_flow_insights] wrote: {out}")
    print(f"[enrich_kr_flow_insights] insight markets: "
          f"{[k for k in insights if k not in ('data_date', 'unit')]}")
    # Echo a sign-correctness proof line per market/foreign.
    for mk in ("KOSPI", "KOSDAQ"):
        f = (insights.get(mk) or {}).get("foreign")
        if f:
            print(f"[enrich_kr_flow_insights] {mk} 외국인: "
                  f"{f['streak_days']}일 연속 {f['direction']} "
                  f"cum5={f['cum5']} cum20={f['cum20']}")


if __name__ == "__main__":
    main()
