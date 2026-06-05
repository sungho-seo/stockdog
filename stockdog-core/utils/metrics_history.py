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
    conn.commit()
    return conn


def save_indicators(indicators: dict, db_path=DB_PATH) -> None:
    today = date.today().isoformat()
    fg  = indicators.get('fear_and_greed', {}).get('score')
    vix = indicators.get('vix', {}).get('price')
    y10 = indicators.get('us_10y_yield', {}).get('price')
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO market_metrics (date, fg_score, vix, us_10y) VALUES (?,?,?,?)",
            (today, fg, vix, y10)
        )


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
