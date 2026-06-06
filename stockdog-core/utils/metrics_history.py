import os
import json
import sqlite3
import logging
from datetime import datetime, date

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

logger = logging.getLogger(__name__)

DB_PATH = "/app/cache/metrics_history.db"

BG = '#16213E'
GRID = '#1F2E54'
COLORS = {'fg': '#FFD54F', 'vix': '#EF5350', 'y10': '#42A5F5'}


# IMPR-061: macro columns added to market_metrics via guarded migration.
# IMPR-068: hy_spread (daily) and jobless (weekly, sparse) added.
# (Monthly series — CPI/PPI/PCE/UNRATE — do NOT live here; they go in macro_monthly.)
_MACRO_COLUMNS = [
    "us_2y", "us_30y", "t10y2y", "fed_funds", "dxy_broad", "usd_krw", "macro_10y",
    "hy_spread",  # IMPR-068: ICE BofA US HY OAS (daily)
    "jobless",    # IMPR-068: Initial Jobless Claims (weekly, stored sparse)
    # IMPR-070: VIX sourced from FRED VIXCLS (daily). Replaces yfinance-sourced
    # vix column on market_metrics for the macro daily series. Note: the existing
    # `vix` column (populated by save_indicators from yfinance ^VIX) is reused —
    # save_macro now also writes vix via FRED VIXCLS to the same column on backfill
    # and daily runs, so the column is shared (last writer wins; FRED data is
    # authoritative for historical depth).
]


def _migrate_market_metrics(conn) -> None:
    """IMPR-061 M2: idempotent PRAGMA-guarded ADD COLUMN migration.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, so we read the live schema via
    `PRAGMA table_info` and ALTER only for columns that are missing. Safe to run
    on every connection: legacy rows (the original 18) simply get NULL for any
    newly-added column. New columns are all nullable REAL with no default.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(market_metrics)").fetchall()}
    for col in _MACRO_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE market_metrics ADD COLUMN {col} REAL")


def _conn(db_path=DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_metrics (
            date     TEXT PRIMARY KEY,
            fg_score REAL,
            vix      REAL,
            us_10y   REAL
        )
    """)
    # IMPR-061 M2: bring legacy/new DBs up to the macro schema (idempotent).
    _migrate_market_metrics(conn)
    # IMPR-061: monthly inflation series live in their own observation-keyed table
    # (one row per (series, obs_date)) so YoY can be matched by calendar month.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macro_monthly (
            series   TEXT,
            obs_date TEXT,
            level    REAL,
            PRIMARY KEY (series, obs_date)
        )
    """)
    conn.commit()
    return conn


def save_indicators(indicators: dict, db_path=DB_PATH) -> None:
    """IMPR-061 M1: column-scoped, order-independent write.

    The legacy `INSERT OR REPLACE` deleted+reinserted the whole row, which would
    wipe any macro columns written by `save_macro` earlier in the same run. We
    now ensure the row exists, then UPDATE only the F&G/VIX/10Y columns — so the
    two writers never clobber each other regardless of call order.
    """
    today = date.today().isoformat()
    fg  = indicators.get('fear_and_greed', {}).get('score')
    vix = indicators.get('vix', {}).get('price')
    y10 = indicators.get('us_10y_yield', {}).get('price')
    with _conn(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO market_metrics (date) VALUES (?)", (today,))
        conn.execute(
            "UPDATE market_metrics SET fg_score=?, vix=?, us_10y=? WHERE date=?",
            (fg, vix, y10, today)
        )


def save_macro(macro_latest: dict, usd_krw, db_path=DB_PATH) -> None:
    """IMPR-061: persist today's macro snapshot — column-scoped (M1-safe).

    `macro_latest` is {key: {"date", "value"}} from fred_macro.get_macro_latest().
    Daily series (the 7 macro cols) land on today's market_metrics row via a
    column-scoped UPDATE so they never collide with save_indicators. Monthly
    inflation series (cpi/core_cpi/ppi) upsert into macro_monthly keyed by their
    OWN observation date, so the level is filed under the real release month.

    Never raises (the pipeline wraps this too, but defense-in-depth).
    """
    if not macro_latest and usd_krw is None:
        return
    today = date.today().isoformat()
    macro_latest = macro_latest or {}
    try:
        with _conn(db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO market_metrics (date) VALUES (?)", (today,))

            # Daily/weekly macro columns onto today's row — only those we actually have.
            # IMPR-068: hy_spread added to daily; jobless (weekly) stored sparse on
            # its FRED observation date (not today) so the chart shows the right week.
            # IMPR-070: vix (FRED VIXCLS) added — same column as save_indicators' vix,
            # column-scoped UPDATE so they never clobber each other.
            daily_cols = ["us_2y", "macro_10y", "us_30y", "t10y2y", "fed_funds", "dxy_broad",
                          "hy_spread", "vix"]
            for col in daily_cols:
                entry = macro_latest.get(col)
                if entry and entry.get("value") is not None:
                    conn.execute(
                        f"UPDATE market_metrics SET {col}=? WHERE date=?",
                        (entry["value"], today)
                    )
            if usd_krw is not None:
                conn.execute(
                    "UPDATE market_metrics SET usd_krw=? WHERE date=?",
                    (usd_krw, today)
                )
            # jobless (ICSA): file on the FRED observation date (latest Thursday),
            # not today — this keeps chart dates aligned to the actual release week.
            entry = macro_latest.get("jobless")
            if entry and entry.get("value") is not None and entry.get("date"):
                obs_d = entry["date"]
                conn.execute("INSERT OR IGNORE INTO market_metrics (date) VALUES (?)", (obs_d,))
                conn.execute(
                    "UPDATE market_metrics SET jobless=? WHERE date=?",
                    (entry["value"], obs_d)
                )

            # Monthly series → macro_monthly, keyed by observation date.
            # IMPR-068: pce (Core PCE, YoY-computed same as CPI) and unrate added.
            for series in ("cpi", "core_cpi", "ppi", "pce", "unrate"):
                entry = macro_latest.get(series)
                if entry and entry.get("value") is not None and entry.get("date"):
                    conn.execute(
                        "INSERT OR REPLACE INTO macro_monthly (series, obs_date, level) "
                        "VALUES (?,?,?)",
                        (series, entry["date"], entry["value"])
                    )
    except Exception as e:
        logger.warning(f"save_macro failed, ignoring: {e}")


def stage_metrics_snapshot(snapshot_path: str, db_path=DB_PATH, days=30) -> str | None:
    """Stage a vault-readable JSON snapshot of recent market_metrics rows.

    The renderer (render_m7_tracker.py) is host-side & stdlib-only and must NOT
    read the root-owned, gitignored metrics_history.db. This function runs
    container-side (where the DB is readable) at the same point save_indicators
    runs, and dumps the last `days` rows into a small JSON the renderer can read.

    Output: snapshot_path (e.g. /notes/raw/stockdog/m7/metrics_snapshot.json).
    Content: {"updated": "<today>", "order": "oldest->newest",
              "series": [{"date","fg_score","vix","us_10y"}, ...]}.
    Atomic write (tmp + os.replace), mirroring the m7_store pattern.

    If the DB is missing/empty, writes a snapshot with an empty series (the
    renderer handles absence/empty gracefully). Returns the path, or None on
    failure (caller wraps in try/except so this never breaks the pipeline).
    """
    try:
        with _conn(db_path) as conn:
            rows = conn.execute(
                "SELECT date, fg_score, vix, us_10y FROM market_metrics "
                "ORDER BY date DESC LIMIT ?", (days,)
            ).fetchall()
        # Reverse to oldest->newest so the renderer can sparkline left->right.
        rows = sorted(rows)
        series = [
            {"date": r[0], "fg_score": r[1], "vix": r[2], "us_10y": r[3]}
            for r in rows
        ]
        payload = {
            "updated": date.today().isoformat(),
            "order": "oldest->newest",
            "series": series,
        }
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        tmp = snapshot_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, snapshot_path)
        logger.info(f"Metrics snapshot staged: {snapshot_path} [{len(series)} rows]")
        return snapshot_path
    except Exception as e:
        logger.warning(f"stage_metrics_snapshot failed, ignoring: {e}")
        return None


def _add_months(yyyy_mm: tuple, delta: int) -> tuple:
    """(year, month) +/- delta months → (year, month). delta may be negative."""
    y, m = yyyy_mm
    total = (y * 12 + (m - 1)) + delta
    return total // 12, total % 12 + 1


def _compute_inflation(rows):
    """IMPR-061 M3: YoY by CALENDAR-MONTH match, not positional row offset.

    `rows` = [(obs_date, level), ...] ascending for one monthly series.
    For each observation we look up the level exactly 12 calendar months prior
    (matched by (year, month)); YoY = latest/prior − 1. If the −12mo month is
    absent, that point yields no YoY. If fewer than 13 distinct months exist,
    the latest YoY is None (renderer shows "—").

    Returns {"latest": {"date","level","yoy"}|None, "history": [{"date","yoy"}...]}.
    """
    if not rows:
        return {"latest": None, "history": []}

    # level keyed by (year, month); last write wins for dup months (shouldn't happen).
    by_month = {}
    for obs_date, level in rows:
        try:
            y, m = int(obs_date[:4]), int(obs_date[5:7])
        except (ValueError, IndexError):
            continue
        by_month[(y, m)] = (obs_date, level)

    history = []
    for (y, m) in sorted(by_month):
        obs_date, level = by_month[(y, m)]
        prior_key = _add_months((y, m), -12)
        prior = by_month.get(prior_key)
        yoy = None
        if prior and prior[1] not in (None, 0):
            yoy = round((level / prior[1] - 1) * 100, 2)
        if yoy is not None:
            history.append({"date": obs_date, "yoy": yoy})

    latest = None
    # latest observation overall (by month order)
    last_key = sorted(by_month)[-1]
    last_date, last_level = by_month[last_key]
    distinct_months = len(by_month)
    last_prior = by_month.get(_add_months(last_key, -12))
    if distinct_months >= 13 and last_prior and last_prior[1] not in (None, 0):
        last_yoy = round((last_level / last_prior[1] - 1) * 100, 2)
    else:
        last_yoy = None
    latest = {"date": last_date, "level": last_level, "yoy": last_yoy}
    return {"latest": latest, "history": history}


def stage_macro_snapshot(snapshot_path: str, db_path=DB_PATH) -> str | None:
    """IMPR-061: stage a vault-readable macro JSON for the host-side renderer.

    The renderer (render_macro_tracker.py) is host-side & stdlib-only and must
    NOT read the root-owned, gitignored metrics_history.db. This runs
    container-side and dumps:

      {"updated": <today>, "order": "oldest->newest",
       "daily": [{date, us_2y, macro_10y, us_30y, t10y2y, fed_funds, dxy_broad,
                  usd_krw, hy_spread, jobless, vix} ... last 400, oldest->newest],
       -- IMPR-070: LIMIT raised 90→400 (~1.5yr trading days) for period-toggle
       "inflation": {cpi: {latest:{date,level,yoy}, history:[{date,yoy}...]},
                     core_cpi: {...}, ppi: {...},
                     pce: {...},        ← IMPR-068: Core PCE YoY
                     unrate: {latest:{date,level,yoy}, history:[{date,yoy}...],
                              level_history:[{date,level}...]}}}
                               ← IMPR-069: unemployment rate level history for charting

    YoY is computed by calendar-month match (M3). Atomic write (tmp + os.replace),
    ensure_ascii=False. Never raises (caller wraps too).
    """
    try:
        with _conn(db_path) as conn:
            daily_rows = conn.execute(
                # IMPR-070: LIMIT raised 90→400 (~1.5yr trading days) for period-toggle;
                # vix added (FRED VIXCLS source via save_macro / backfill_macro).
                "SELECT date, us_2y, macro_10y, us_30y, t10y2y, fed_funds, dxy_broad, usd_krw,"
                "       hy_spread, jobless, vix "
                "FROM market_metrics ORDER BY date DESC LIMIT 400"
            ).fetchall()
            daily_rows = sorted(daily_rows)  # oldest -> newest
            daily = [
                {
                    "date": r[0], "us_2y": r[1], "macro_10y": r[2], "us_30y": r[3],
                    "t10y2y": r[4], "fed_funds": r[5], "dxy_broad": r[6], "usd_krw": r[7],
                    "hy_spread": r[8], "jobless": r[9], "vix": r[10],
                }
                for r in daily_rows
            ]

            inflation = {}
            # CPI/core_cpi/PPI/PCE: YoY computed via _compute_inflation (calendar-month match).
            # UNRATE: level stored in macro_monthly but rendered as level (no YoY), so we
            #   pass it through _compute_inflation too — the "yoy" field will be present but
            #   the renderer can choose to display "level" instead. We store level as-is.
            for series in ("cpi", "core_cpi", "ppi", "pce", "unrate"):
                mrows = conn.execute(
                    "SELECT obs_date, level FROM macro_monthly WHERE series=? ORDER BY obs_date ASC",
                    (series,)
                ).fetchall()
                inflation[series] = _compute_inflation([(r[0], r[1]) for r in mrows])

            # IMPR-069: unrate level_history — monthly level time-series for charting.
            # _compute_inflation only emits history[].yoy (CPI/PPI/PCE pattern); unrate
            # must be charted as a level, not YoY. We add level_history: [{date, level}]
            # covering ALL available months so the dashboard chart gets ≥24 data points.
            unrate_lrows = conn.execute(
                "SELECT obs_date, level FROM macro_monthly WHERE series='unrate' ORDER BY obs_date ASC"
            ).fetchall()
            inflation["unrate"]["level_history"] = [
                {"date": r[0], "level": r[1]}
                for r in unrate_lrows
                if r[1] is not None
            ]

        payload = {
            "updated": date.today().isoformat(),
            "order": "oldest->newest",
            "daily": daily,
            "inflation": inflation,
        }
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        tmp = snapshot_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, snapshot_path)
        logger.info(f"Macro snapshot staged: {snapshot_path} [{len(daily)} daily rows]")
        return snapshot_path
    except Exception as e:
        logger.warning(f"stage_macro_snapshot failed, ignoring: {e}")
        return None


def generate_trend_chart(media_dir: str, date_str: str, db_path=DB_PATH, days=30):
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT date, fg_score, vix, us_10y FROM market_metrics "
            "ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()

    if len(rows) < 2:
        logger.info("Not enough data for trend chart yet, skipping.")
        return None

    rows = sorted(rows)
    dates = [datetime.strptime(r[0], "%Y-%m-%d") for r in rows]
    fg    = [r[1] for r in rows]
    vix   = [r[2] for r in rows]
    y10   = [r[3] for r in rows]

    fig, axes = plt.subplots(3, 1, figsize=(10, 7), facecolor=BG,
                              gridspec_kw={'hspace': 0.5})

    specs = [
        (axes[0], fg,  'Fear & Greed', COLORS['fg'],  (0, 100)),
        (axes[1], vix, 'VIX',          COLORS['vix'], None),
        (axes[2], y10, 'US 10Y Yield', COLORS['y10'], None),
    ]

    for ax, values, title, color, ylim in specs:
        ax.set_facecolor(BG)
        valid = [(d, v) for d, v in zip(dates, values) if v is not None]
        if valid:
            dx, vx = zip(*valid)
            ax.plot(dx, vx, color=color, linewidth=1.8,
                    marker='o', markersize=3.5, markerfacecolor=color)
            ax.fill_between(dx, vx, alpha=0.12, color=color)
        ax.set_title(title, color='white', fontsize=10, fontweight='bold', pad=4)
        ax.tick_params(colors='#90A4AE', labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor(GRID)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=7))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right')
        ax.grid(axis='y', color=GRID, linewidth=0.8)
        if ylim:
            ax.set_ylim(*ylim)

    fig.suptitle(f"Market Indicators — Last {len(rows)} Days  ({date_str})",
                 color='white', fontsize=11, fontweight='bold', y=1.01)

    os.makedirs(media_dir, exist_ok=True)
    filepath = os.path.join(media_dir, f"trend_{date_str}.png")
    plt.savefig(filepath, dpi=130, bbox_inches='tight',
                facecolor=BG, edgecolor='none')
    plt.close(fig)
    logger.info(f"Trend chart saved: {filepath}")
    return filepath


def append_chart_to_report(report_path: str, chart_filename: str) -> None:
    section = f"\n\n---\n## 📈 Market Indicators Trend\n\n![[media/{chart_filename}]]\n"
    with open(report_path, 'a', encoding='utf-8') as f:
        f.write(section)
