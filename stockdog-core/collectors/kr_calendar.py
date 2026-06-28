"""KR market calendar — 주요 일정 (P2/Phase D of the 국장/KR page).

Produces an UPCOMING-events list (금통위 / 네마녀의 날 / MSCI 리밸런싱) for the
/kr dashboard. Mirrors collectors/economic_calendar.py's structure (baked policy
constants + pure date math for the computable event), but is KR-specific and
needs NO network — every event is either a deterministic date computation or a
baked constant verified from an official source.

⚠️ DATA INTEGRITY (hardcoded calendar dates have burned us before — US FOMC
dates were once wrong). The rules:
  * COMPUTED (deterministic, preferred): 네마녀의 날 (지수·개별주식 선·옵션
    동시만기 / quad-witching) = 2nd Thursday of Mar/Jun/Sep/Dec. Pure stdlib
    date math, like economic_calendar.quad_witching_dates (US uses 3rd Friday;
    KRX uses the 2nd Thursday).
  * BAKED + VERIFIED (NOT computable, NEVER guessed): 금통위(BOK MPC) and MSCI
    semi-annual review dates. Sourced below; update each January. A wrong or
    unverifiable date must be OMITTED rather than guessed.

Sources (verified 2026-06-28):
  * 금통위 2026 통화정책방향 결정회의 — bok.or.kr 통화정책방향 결정회의 목록
    https://www.bok.or.kr/portal/singl/crncyPolicyDrcMtg/listYear.do?mtgSe=A&menuNo=200755
    1/15, 2/26, 4/10, 5/28, 7/16, 8/27, 10/22, 11/26 (all Thu except 4/10 Fri).
    Cross-checked: newsis/edaily reported the 8 meeting MONTHS (1·2·4·5·7·8·10·11)
    and the 7/16·8/27·10/22·11/26 future dates were corroborated by news search.
  * MSCI 2026 Semi-Annual Index Review (반기 리밸런싱) — msci.com index-review.
    May 2026 review (announce 5/12, effective close 5/29) ALREADY PASSED.
    November 2026 review: announce 11/11, changes EFFECTIVE 2026-12-01.
    https://www.msci.com/indexes/index-resources/index-review

The 금통위/MSCI baked lists are YEAR-SCOPED — once they all fall into the past
(e.g. running in 2027 before next January's update) the calendar simply ships
fewer/zero events, degrading gracefully (mirrors FOMC_DATES_2027 = []).
"""
import json
import logging
import os
from datetime import datetime, timedelta, date, timezone

logger = logging.getLogger(__name__)

# 금통위 (BOK MPC rate-decision) — baked official schedule. UPDATE EACH JANUARY
# from bok.or.kr. type tag = "rate".
BOK_MPC_DATES = [
    "2026-01-15", "2026-02-26", "2026-04-10", "2026-05-28",
    "2026-07-16", "2026-08-27", "2026-10-22", "2026-11-26",
]

# MSCI 반기 리밸런싱 (semi-annual review EFFECTIVE date). Baked from MSCI's
# published schedule; the May 2026 review already passed, November's changes
# take effect 2026-12-01. UPDATE EACH YEAR. type tag = "rebalance".
MSCI_REBALANCE_DATES = [
    "2026-12-01",   # Nov 2026 SAIR effective (announce 2026-11-11)
]


def kr_quad_witching_dates(year):
    """네마녀의 날 (지수선물·지수옵션·개별주식 선물·옵션 동시만기) =
    2nd Thursday of Mar/Jun/Sep/Dec. Pure stdlib date math — no network.

    Returns a list of "YYYY-MM-DD" strings. The 2nd Thursday is found by walking
    from the 1st to the first Thursday (weekday()==3) then +7 days.
    """
    out = []
    for month in (3, 6, 9, 12):
        d = date(year, month, 1)
        offset = (3 - d.weekday()) % 7   # days until the first Thursday (Thu=3)
        second_thu = d + timedelta(days=offset + 7)
        out.append(second_thu.strftime("%Y-%m-%d"))
    return out


def _kst_today():
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


def _days_until(date_str, today):
    return (datetime.strptime(date_str, "%Y-%m-%d").date() - today).days


def get_kr_calendar(today=None):
    """Build the KR 주요 일정 upcoming-events model.

    Collects 금통위(rate) + 네마녀의 날(expiry) + MSCI 리밸런싱(rebalance), keeps
    ONLY FUTURE events (date >= today), de-dupes, sorts by date, and stamps a
    days_until countdown. Pure date math + baked, VERIFIED constants — NO network,
    NO LLM. NEVER raises (mirrors economic_calendar.get_this_week_calendar).

    Returns:
      {"data_date": "<today ISO>", "events": [{name,date,type,days_until}, ...]}
      where type ∈ {"rate","expiry","rebalance"}. Tolerant: any failure → empty
      events list, so the emitter simply hides the card.
    """
    try:
        today = today or _kst_today()
        if isinstance(today, str):
            today = datetime.strptime(today, "%Y-%m-%d").date()
        today_str = today.strftime("%Y-%m-%d")

        raw = []
        for d in BOK_MPC_DATES:
            raw.append({"name": "금통위", "date": d, "type": "rate"})
        # 네마녀 — current year + next, so the Dec→Mar rollover always has a
        # future witching date even late in December.
        for d in kr_quad_witching_dates(today.year) + kr_quad_witching_dates(today.year + 1):
            raw.append({"name": "네마녀의 날", "date": d, "type": "expiry"})
        for d in MSCI_REBALANCE_DATES:
            raw.append({"name": "MSCI 리밸런싱", "date": d, "type": "rebalance"})

        events = []
        seen = set()
        for e in raw:
            if e["date"] < today_str:        # FUTURE only — past events drop off
                continue
            key = (e["name"], e["date"])
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "name": e["name"], "date": e["date"], "type": e["type"],
                "days_until": _days_until(e["date"], today),
            })
        events.sort(key=lambda e: e["date"])
        return {"data_date": today_str, "events": events}
    except Exception as e:
        logger.warning(f"get_kr_calendar failed, returning empty: {e}")
        return {"data_date": None, "events": []}


def stage_kr_calendar(vault_root, today=None):
    """Atomic-write the KR calendar JSON into the vault (optional host-side
    staging entrypoint; the pipeline normally carries it as a collect() key).

    Output: <vault_root>/raw/stockdog/kr/kr_calendar.json. Never raises.
    """
    try:
        payload = get_kr_calendar(today=today)
        cal_dir = os.path.join(vault_root, "raw", "stockdog", "kr")
        os.makedirs(cal_dir, exist_ok=True)
        out_path = os.path.join(cal_dir, "kr_calendar.json")
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out_path)
        logger.info(
            f"KR calendar staged: {out_path} [{len(payload.get('events', []))} events]"
        )
        return out_path
    except Exception as e:
        logger.warning(f"stage_kr_calendar failed, ignoring: {e}")
        return None


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Build/stage the KR 주요 일정 calendar.")
    ap.add_argument("--stage", metavar="VAULT_ROOT", default=None,
                    help="vault root; writes raw/stockdog/kr/kr_calendar.json")
    ap.add_argument("--today", default=None,
                    help="override 'today' as YYYY-MM-DD (testing/dry-run)")
    args = ap.parse_args()
    if args.stage:
        path = stage_kr_calendar(args.stage, today=args.today)
        raise SystemExit(0 if path else 1)
    print(json.dumps(get_kr_calendar(today=args.today), ensure_ascii=False, indent=2))
