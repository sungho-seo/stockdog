import os
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"

FRED_SERIES = {
    "CPI":      "CPIAUCSL",
    "Core CPI": "CPILFESL",
    "PPI":      "PPIACO",
}

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
