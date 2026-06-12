import os
import json
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"

FRED_SERIES = {
    "CPI":      "CPIAUCSL",
    "Core CPI": "CPILFESL",
    "PPI":      "PPIACO",
    # IMPR-0xx (주요 일정 chips): NFP (employment situation, PAYEMS) and
    # PCE (PCE price index, PCEPI) added so the this-week calendar can surface
    # their next release dates. Reuses _next_release_date like the others.
    "NFP":      "PAYEMS",
    "PCE":      "PCEPI",
}

# 주요 일정 calendar — importance per event drives overflow priority when the
# header caps at 3 chips. PPI is the only medium-importance release; the rest
# (CPI/Core CPI/NFP/PCE/FOMC/witching) are high.
_CAL_IMPORTANCE = {
    "CPI": "high", "Core CPI": "high", "NFP": "high", "PCE": "high",
    "PPI": "med", "FOMC": "high", "네마녀": "high",
}
# Korean weekday labels indexed by datetime.weekday() (Mon=0 .. Sun=6).
_KR_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

# Annual schedule — update each January from https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
FOMC_DATES_2026 = [
    "2026-01-28", "2026-03-18", "2026-05-06",
    "2026-06-17", "2026-07-29", "2026-09-16",
    "2026-10-28", "2026-12-16",
]

# IMPR-061: 2027 schedule not yet published by the Fed as of build time.
# Leave empty so the macro renderer's FOMC countdown degrades gracefully
# (M6 fallback) instead of guessing. Populate each January from
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
FOMC_DATES_2027 = []  # TODO confirm from federalreserve.gov


def _fred(endpoint, params):
    params = {**params, "api_key": os.getenv("FRED_API_KEY"), "file_type": "json"}
    resp = requests.get(f"{FRED_BASE}/{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _latest_observation(series_id):
    data = _fred("series/observations", {
        "series_id": series_id,
        "observation_start": "2024-01-01",
        "sort_order": "desc",
        "limit": 1,
    })
    obs = [o for o in data.get("observations", []) if o["value"] != "."]
    if not obs:
        return None, None
    return obs[0]["date"], round(float(obs[0]["value"]), 3)


def _next_release_date(series_id, from_date):
    rel = _fred("series/release", {"series_id": series_id})
    release_id = rel["releases"][0]["id"]
    data = _fred("release/dates", {
        "release_id": release_id,
        "realtime_start": from_date,
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc",
        "limit": 3,
    })
    dates = data.get("release_dates", [])
    return dates[0]["date"] if dates else None


def get_economic_calendar(sample=False):
    """
    2A: upcoming events within 7 days (name, date, days_until, prev_actual)
    2B: releasing_today — calendar-based (next_release == today)
    Never raises; sets error field on failure.
    """
    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    week_end = (today + timedelta(days=7)).strftime("%Y-%m-%d")
    result = {"upcoming": [], "releasing_today": [], "error": None}

    for d in FOMC_DATES_2026:
        days_until = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
        if today_str <= d <= week_end:
            result["upcoming"].append({
                "name": "FOMC Meeting", "date": d,
                "days_until": days_until, "prev_actual": "N/A", "consensus": "N/A",
            })
        if d == today_str:
            result["releasing_today"].append({
                "name": "FOMC Meeting", "date": d, "consensus": "N/A",
            })

    if sample:
        print("[SAMPLE] Economic calendar: FOMC only, skipping FRED API")
        return result

    if not os.getenv("FRED_API_KEY"):
        result["error"] = "FRED_API_KEY not set"
        logger.warning("Economic calendar: FRED_API_KEY not set, skipping")
        return result

    for name, series_id in FRED_SERIES.items():
        try:
            prev_date, prev_actual = _latest_observation(series_id)
            next_release = _next_release_date(series_id, today_str)
        except Exception as e:
            logger.warning(f"FRED fetch failed for {name}: {e}")
            continue

        if next_release and today_str <= next_release <= week_end:
            days_until = (datetime.strptime(next_release, "%Y-%m-%d").date() - today).days
            result["upcoming"].append({
                "name": name, "date": next_release, "days_until": days_until,
                "prev_actual": prev_actual, "prev_date": prev_date, "consensus": "N/A",
            })

        if next_release == today_str:
            result["releasing_today"].append({
                "name": name, "date": today_str,
                "prev_actual": prev_actual, "consensus": "N/A",
            })

    result["upcoming"].sort(key=lambda x: x["date"])
    return result


# ---------------------------------------------------------------------------
# 주요 일정 (this-week calendar chips) — IMPR-0xx
# ---------------------------------------------------------------------------
def quad_witching_dates(year):
    """Quad-witching (네마녀의 날) = 3rd Friday of Mar/Jun/Sep/Dec.

    Pure stdlib date math — no network. Returns a list of "YYYY-MM-DD" strings.
    The 3rd Friday is found by walking from the 1st of the month to the first
    Friday (weekday()==4), then +14 days.
    """
    out = []
    for month in (3, 6, 9, 12):
        d = datetime(year, month, 1).date()
        # days until the first Friday (Mon=0..Sun=6; Fri=4)
        offset = (4 - d.weekday()) % 7
        third_friday = d + timedelta(days=offset + 14)
        out.append(third_friday.strftime("%Y-%m-%d"))
    return out


def _kr_weekday(date_str):
    """'YYYY-MM-DD' → Korean weekday label (월..일)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return _KR_WEEKDAYS[d.weekday()]


def get_this_week_calendar(today=None):
    """Build the 주요 일정 data model for the CURRENT week (Mon–Fri).

    Collects, for the Mon–Fri window containing `today`:
      * FOMC meeting days (FOMC_DATES_2026)
      * FRED next-release dates for CPI/Core CPI/PPI/NFP/PCE
      * quad-witching (네마녀) days
    Only events whose date is within [monday, friday] are included. Each event
    gets a pre-computed Korean weekday, a type (release|fomc|witching), and an
    importance (high|med). Sorted by date.

    Tolerant: any FRED failure → that event is simply absent; `error` captures a
    message but the function NEVER raises (mirrors get_economic_calendar).

    Returns:
      {"updated": "<today>", "window": {"start","end"}, "events": [...],
       "error": None|"<msg>"}
    """
    today = today or datetime.now().date()
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d").date()

    monday = today - timedelta(days=today.weekday())  # Mon of this week
    friday = monday + timedelta(days=4)
    mon_str, fri_str = monday.strftime("%Y-%m-%d"), friday.strftime("%Y-%m-%d")

    def in_window(d):
        return mon_str <= d <= fri_str

    result = {
        "updated": today.strftime("%Y-%m-%d"),
        "window": {"start": mon_str, "end": fri_str},
        "events": [],
        "error": None,
    }

    # FOMC — calendar constant, no network.
    for d in FOMC_DATES_2026:
        if in_window(d):
            result["events"].append({
                "name": "FOMC", "date": d, "weekday": _kr_weekday(d),
                "type": "fomc", "importance": _CAL_IMPORTANCE["FOMC"],
            })

    # 네마녀 (quad-witching) — pure date math, no network.
    for d in quad_witching_dates(today.year):
        if in_window(d):
            result["events"].append({
                "name": "네마녀", "date": d, "weekday": _kr_weekday(d),
                "type": "witching", "importance": _CAL_IMPORTANCE["네마녀"],
            })

    # FRED releases — each independently tolerant.
    if not os.getenv("FRED_API_KEY"):
        result["error"] = "FRED_API_KEY not set"
        logger.warning("This-week calendar: FRED_API_KEY not set, FRED events skipped")
    else:
        from_date = mon_str  # earliest date we care about
        for name, series_id in FRED_SERIES.items():
            try:
                next_release = _next_release_date(series_id, from_date)
            except Exception as e:
                logger.warning(f"This-week calendar: FRED fetch failed for {name}: {e}")
                if result["error"] is None:
                    result["error"] = f"FRED fetch failed for {name}: {e}"
                continue
            if next_release and in_window(next_release):
                result["events"].append({
                    "name": name, "date": next_release,
                    "weekday": _kr_weekday(next_release),
                    "type": "release",
                    "importance": _CAL_IMPORTANCE.get(name, "high"),
                })

    result["events"].sort(key=lambda e: e["date"])
    return result


def stage_this_week_calendar(vault_root, today=None):
    """Atomic-write the this-week calendar JSON into the vault.

    Output: <vault_root>/raw/stockdog/calendar/this_week.json
    Atomic (tmp + os.replace), mkdir -p the calendar dir. Mirrors
    utils/metrics_history.py::stage_macro_snapshot. Never raises — returns the
    written path, or None on failure (caller wraps too).
    """
    try:
        payload = get_this_week_calendar(today=today)
        cal_dir = os.path.join(vault_root, "raw", "stockdog", "calendar")
        os.makedirs(cal_dir, exist_ok=True)
        out_path = os.path.join(cal_dir, "this_week.json")
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out_path)
        logger.info(
            f"This-week calendar staged: {out_path} "
            f"[{len(payload.get('events', []))} events]"
        )
        return out_path
    except Exception as e:
        logger.warning(f"stage_this_week_calendar failed, ignoring: {e}")
        return None


if __name__ == "__main__":
    # Host-side staging entrypoint (preferred wiring — no docker rebuild):
    #   python3 -m collectors.economic_calendar --stage <vault_root> [--today YYYY-MM-DD]
    # FRED_API_KEY is read from the environment (sync_vault.sh sources ../.env
    # before calling this). Exits 0 on success, 1 if nothing was staged.
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Stage the 주요 일정 this-week calendar.")
    ap.add_argument("--stage", metavar="VAULT_ROOT", required=True,
                    help="vault root; writes raw/stockdog/calendar/this_week.json")
    ap.add_argument("--today", default=None,
                    help="override 'today' as YYYY-MM-DD (testing/dry-run)")
    args = ap.parse_args()
    path = stage_this_week_calendar(args.stage, today=args.today)
    if path:
        print(f"[economic_calendar] staged {path}")
        raise SystemExit(0)
    print("[economic_calendar] staging failed", flush=True)
    raise SystemExit(1)
