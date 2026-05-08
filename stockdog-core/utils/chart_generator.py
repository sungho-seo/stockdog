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
    Generates a dark-themed speedometer gauge chart for the Fear & Greed Index.
    """
    try:
        # Timezones
        kst = pytz.timezone('Asia/Seoul')
        est = pytz.timezone('US/Eastern')
        
        now_kst = datetime.now(kst)
        now_est = datetime.now(est)
        
        if date_str is None:
            date_str = now_kst.strftime("%Y-%m-%d")
        
        time_info = f"KST: {now_kst.strftime('%H:%M')} | EST: {now_est.strftime('%H:%M')}"

        BG = '#16213E'
        fig, ax = plt.subplots(figsize=(8, 5.5), facecolor=BG)
        ax.set_facecolor(BG)
        ax.set_xlim(-1.25, 1.25)
        ax.set_ylim(-0.45, 1.3)
        ax.set_aspect('equal')
        ax.axis('off')

        # ── Gauge zones ──────────────────────────────────────────────
        for s_start, s_end, color, label in GAUGE_ZONES:
            a_start = 180 - (s_start / 100) * 180
            a_end   = 180 - (s_end   / 100) * 180
            wedge = mpatches.Wedge(
                center=(0, 0), r=1.0,
                theta1=a_end, theta2=a_start,
                width=0.32,
                facecolor=color, edgecolor=BG, linewidth=2.5,
                zorder=2
            )
            ax.add_patch(wedge)

            # Zone label (Increased font size)
            mid_a = np.radians(180 - ((s_start + s_end) / 2 / 100) * 180)
            r_lbl = 0.77
            ax.text(
                r_lbl * np.cos(mid_a), r_lbl * np.sin(mid_a),
                label, ha='center', va='center',
                fontsize=10.5, color='white', fontweight='bold',
                multialignment='center', zorder=3
            )

        # ── Outer / inner ring ────────────────────────────────────────
        ring_outer = mpatches.Wedge((0,0), r=1.02, theta1=0, theta2=180,
                                    width=0.01, facecolor='#ECEFF1', zorder=4)
        ring_inner = mpatches.Wedge((0,0), r=0.70, theta1=0, theta2=180,
                                    width=0.01, facecolor='#ECEFF1', zorder=4)
        ax.add_patch(ring_outer)
        ax.add_patch(ring_inner)

        # ── Tick marks (10-point intervals) ──────────────────────────
        for tick_score in range(0, 101, 10):
            tick_a = np.radians(180 - (tick_score / 100) * 180)
            r_in, r_out = 0.71, 0.76
            ax.plot(
                [r_in * np.cos(tick_a), r_out * np.cos(tick_a)],
                [r_in * np.sin(tick_a), r_out * np.sin(tick_a)],
                color='white', lw=1.2, zorder=5
            )

        # ── Needle (Enhanced Arrow Head) ──────────────────────────────
        needle_a  = np.radians(180 - (score / 100) * 180)
        needle_len = 0.72
        
        # Draw the needle line
        ax.plot([0, needle_len * np.cos(needle_a)], [0, needle_len * np.sin(needle_a)],
                color='white', lw=3.5, zorder=6)
        
        # Add a larger triangle arrow head at the tip
        head_len = 0.08
        ax.arrow(0, 0, (needle_len+0.02) * np.cos(needle_a), (needle_len+0.02) * np.sin(needle_a),
                 head_width=0.07, head_length=head_len, fc='white', ec='white', 
                 length_includes_head=True, zorder=7)

        # Needle pivot
        pivot = plt.Circle((0, 0), 0.06, color='white', zorder=8)
        ax.add_patch(pivot)

        # ── Score & rating text ───────────────────────────────────────
        ax.text(0, -0.15, str(int(round(score))),
                ha='center', va='center',
                fontsize=46, color='white', fontweight='bold', zorder=9)

        rating_color = RATING_COLORS.get(rating.lower().strip(), '#ECEFF1')
        ax.text(0, -0.32, rating.upper(),
                ha='center', va='center',
                fontsize=14, color=rating_color, fontweight='bold', zorder=9)

        # ── Title & Date/Time ─────────────────────────────────────────
        ax.text(0, 1.20, 'Fear & Greed Index',
                ha='center', va='center',
                fontsize=16, color='white', fontweight='bold')
        ax.text(0, 1.10, date_str,
                ha='center', va='center',
                fontsize=11, color='#90A4AE', fontweight='bold')
        ax.text(0, 1.02, time_info,
                ha='center', va='center',
                fontsize=9, color='#CFD8DC')

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
