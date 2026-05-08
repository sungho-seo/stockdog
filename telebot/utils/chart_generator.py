import os
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Headless mode (no display needed in Docker)
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from datetime import datetime
import pytz
import logging

logger = logging.getLogger(__name__)

# Zone definitions: (score_start, score_end, hex_color, label)
GAUGE_ZONES = [
    (0,  25, '#C62828', 'Extreme\nFear'),
    (25, 45, '#EF6C00', 'Fear'),
    (45, 55, '#F9A825', 'Neutral'),
    (55, 75, '#558B2F', 'Greed'),
    (75, 100,'#1B5E20', 'Extreme\nGreed'),
]

RATING_COLORS = {
    'extreme fear': '#EF5350',
    'fear':         '#FF8A65',
    'neutral':      '#FFD54F',
    'greed':        '#AED581',
    'extreme greed':'#66BB6A',
}


def generate_fear_greed_gauge(score: float, rating: str, output_dir: str, date_str: str = None) -> str:
    """
    Generates the final premium dark-themed speedometer gauge chart.
    """
    try:
        # Timezones
        kst = pytz.timezone('Asia/Seoul')
        est = pytz.timezone('US/Eastern')
        now_kst = datetime.now(kst)
        now_est = datetime.now(est)
        
        if date_str is None:
            date_str = now_kst.strftime("%Y-%m-%d")
        
        # Combined Date and Time on one line
        header_info = f"{date_str}  (KST {now_kst.strftime('%H:%M')} / EST {now_est.strftime('%H:%M')})"

        BG = '#16213E'
        PIVOT_COLOR = '#1F2E54'
        fig, ax = plt.subplots(figsize=(8, 6), facecolor=BG)
        ax.set_facecolor(BG)
        ax.set_xlim(-1.25, 1.25)
        ax.set_ylim(-0.45, 1.45)
        ax.set_aspect('equal')
        ax.axis('off')

        # ── Gauge zones (Thick 0.38) ──────────────────────────────────
        for s_start, s_end, color, label in GAUGE_ZONES:
            a_start = 180 - (s_start / 100) * 180
            a_end   = 180 - (s_end   / 100) * 180
            wedge = mpatches.Wedge(
                center=(0, 0), r=1.0,
                theta1=a_end, theta2=a_start,
                width=0.38,
                facecolor=color, edgecolor=BG, linewidth=2.0,
                zorder=2
            )
            ax.add_patch(wedge)

            # Zone label
            mid_a = np.radians(180 - ((s_start + s_end) / 2 / 100) * 180)
            r_lbl = 0.81
            ax.text(
                r_lbl * np.cos(mid_a), r_lbl * np.sin(mid_a),
                label.replace('\n', ' '), ha='center', va='center',
                fontsize=10, color='white', fontweight='bold',
                zorder=3
            )

        # ── Detailed Tick Labels (Boundaries) ─────────────────────────
        for tick in [0, 25, 45, 55, 75, 100]:
            tick_a = np.radians(180 - (tick / 100) * 180)
            r_tick = 0.56
            ax.text(
                r_tick * np.cos(tick_a), r_tick * np.sin(tick_a),
                str(tick), ha='center', va='center',
                fontsize=9, color='#B0BEC5', fontweight='bold', zorder=5
            )

        # ── Final Needle Design (Tapered, Blunt tip, Shortened) ────────
        angle = np.radians(180 - (score / 100) * 180)
        pivot_r = 0.10
        needle_len = 0.75 
        
        base_w = 0.025 # Thick base
        tip_w  = 0.008 # Blunt tip
        
        p_angle = angle + np.pi/2
        
        # Coordinates for tapered needle polygon
        points = [
            (pivot_r * np.cos(angle) + base_w * np.cos(p_angle), pivot_r * np.sin(angle) + base_w * np.sin(p_angle)),
            (pivot_r * np.cos(angle) - base_w * np.cos(p_angle), pivot_r * np.sin(angle) - base_w * np.sin(p_angle)),
            (needle_len * np.cos(angle) - tip_w * np.cos(p_angle), needle_len * np.sin(angle) - tip_w * np.sin(p_angle)),
            (needle_len * np.cos(angle) + tip_w * np.cos(p_angle), needle_len * np.sin(angle) + tip_w * np.sin(p_angle)),
        ]
        needle_poly = mpatches.Polygon(points, facecolor='white', edgecolor='white', linewidth=0.1, zorder=10)
        ax.add_patch(needle_poly)

        # Semi-circle Pivot (Instead of full white circle)
        pivot = mpatches.Wedge(
            center=(0, 0), r=pivot_r, theta1=0, theta2=180, 
            facecolor=PIVOT_COLOR, edgecolor='#2C3E66', linewidth=1.5, zorder=12
        )
        ax.add_patch(pivot)

        # ── Score & Rating text ───────────────────────────────────────
        ax.text(0, -0.16, str(int(round(score))),
                ha='center', va='center',
                fontsize=42, color='white', fontweight='bold', zorder=15)

        rating_color = RATING_COLORS.get(rating.lower().strip(), '#ECEFF1')
        ax.text(0, -0.34, rating.upper(),
                ha='center', va='center',
                fontsize=15, color=rating_color, fontweight='bold', zorder=15)

        # ── Header (Title & Combined Date/Time) ───────────────────────
        ax.text(0, 1.35, 'Fear & Greed Index',
                ha='center', va='center',
                fontsize=18, color='white', fontweight='bold')
        ax.text(0, 1.23, header_info,
                ha='center', va='center',
                fontsize=11, color='#90A4AE', fontweight='bold')

        # ── Save ──────────────────────────────────────────────────────
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"fear_greed_{date_str}.png")

        plt.tight_layout(pad=0.3)
        plt.savefig(filepath, dpi=150, bbox_inches='tight',
                    facecolor=BG, edgecolor='none')
        plt.close(fig)

        logger.info(f"Fear & Greed gauge saved to {filepath}")
        return filepath

        # ── Save ──────────────────────────────────────────────────────
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"fear_greed_{date_str}.png")

        plt.tight_layout(pad=0.3)
        plt.savefig(filepath, dpi=150, bbox_inches='tight',
                    facecolor=BG, edgecolor='none')
        plt.close(fig)

        logger.info(f"Fear & Greed gauge saved to {filepath}")
        return filepath

        # ── Edge labels 0 / 100 ───────────────────────────────────────
        ax.text(-1.12, -0.08, '0',   ha='center', fontsize=9, color='#90A4AE')
        ax.text( 1.12, -0.08, '100', ha='center', fontsize=9, color='#90A4AE')

        # ── Save ──────────────────────────────────────────────────────
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"fear_greed_{date_str}.png")

        plt.tight_layout(pad=0.3)
        plt.savefig(filepath, dpi=150, bbox_inches='tight',
                    facecolor=BG, edgecolor='none')
        plt.close(fig)

        logger.info(f"Fear & Greed gauge saved to {filepath}")
        return filepath

    except Exception as e:
        logger.error(f"Failed to generate Fear & Greed gauge: {e}")
        return None

        # ── Edge labels 0 / 100 ───────────────────────────────────────
        ax.text(-1.12, -0.08, '0',   ha='center', fontsize=9, color='#90A4AE')
        ax.text( 1.12, -0.08, '100', ha='center', fontsize=9, color='#90A4AE')

        # ── Save ──────────────────────────────────────────────────────
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"fear_greed_{date_str}.png")

        plt.tight_layout(pad=0.3)
        plt.savefig(filepath, dpi=150, bbox_inches='tight',
                    facecolor=BG, edgecolor='none')
        plt.close(fig)

        logger.info(f"Fear & Greed gauge saved to {filepath}")
        return filepath

    except Exception as e:
        logger.error(f"Failed to generate Fear & Greed gauge: {e}")
        return None


if __name__ == "__main__":
    # Quick test
    path = generate_fear_greed_gauge(
        score=67, rating="greed",
        output_dir="./test_output"
    )
    print(f"Saved: {path}")
