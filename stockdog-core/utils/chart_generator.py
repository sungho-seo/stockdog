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
    Generates a premium dark-themed speedometer gauge chart for the Fear & Greed Index.
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
        fig, ax = plt.subplots(figsize=(8, 5.8), facecolor=BG)
        ax.set_facecolor(BG)
        ax.set_xlim(-1.25, 1.25)
        ax.set_ylim(-0.45, 1.4) # Increased height to prevent title overlap
        ax.set_aspect('equal')
        ax.axis('off')

        # Identify current zone index
        current_zone_idx = 0
        for i, (s_start, s_end, _, _) in enumerate(GAUGE_ZONES):
            if s_start <= score < s_end or (i == len(GAUGE_ZONES)-1 and score >= s_start):
                current_zone_idx = i
                break

        # ── Gauge zones ──────────────────────────────────────────────
        for i, (s_start, s_end, color, label) in enumerate(GAUGE_ZONES):
            a_start = 180 - (s_start / 100) * 180
            a_end   = 180 - (s_end   / 100) * 180
            
            # Highlighting logic: Mute colors if not in the current zone
            is_active = (i == current_zone_idx)
            face_color = color if is_active else '#2A344A' # Muted dark blue-gray for inactive
            edge_color = color if is_active else '#364156'
            alpha = 1.0 if is_active else 0.4
            
            wedge = mpatches.Wedge(
                center=(0, 0), r=1.0,
                theta1=a_end, theta2=a_start,
                width=0.30,
                facecolor=face_color, edgecolor=edge_color, linewidth=1.5,
                alpha=alpha,
                zorder=2
            )
            ax.add_patch(wedge)

            # Zone label (Vibrant if active, muted if not)
            mid_a = np.radians(180 - ((s_start + s_end) / 2 / 100) * 180)
            r_lbl = 0.82
            label_color = 'white' if is_active else '#90A4AE'
            ax.text(
                r_lbl * np.cos(mid_a), r_lbl * np.sin(mid_a),
                label.replace('\n', ' '), ha='center', va='center',
                fontsize=11 if is_active else 9, 
                color=label_color, fontweight='bold',
                zorder=3
            )

        # ── Inner Ring Tick Labels (0, 25, 50, 75, 100) ──────────────
        for tick in [0, 25, 50, 75, 100]:
            tick_a = np.radians(180 - (tick / 100) * 180)
            r_tick = 0.62
            ax.text(
                r_tick * np.cos(tick_a), r_tick * np.sin(tick_a),
                str(tick), ha='center', va='center',
                fontsize=9, color='#78909C', zorder=5
            )

        # ── Needle ────────────────────────────────────────────────────
        needle_a  = np.radians(180 - (score / 100) * 180)
        needle_len = 0.75
        
        # Draw needle line
        ax.plot([0, needle_len * np.cos(needle_a)], [0, needle_len * np.sin(needle_a)],
                color='white', lw=3, zorder=10)
        
        # Larger Arrow Head
        head_len = 0.10
        ax.arrow(0, 0, (needle_len + 0.02) * np.cos(needle_a), (needle_len + 0.02) * np.sin(needle_a),
                 head_width=0.08, head_length=head_len, fc='white', ec='white', 
                 length_includes_head=True, zorder=11)

        # Needle pivot
        pivot = plt.Circle((0, 0), 0.07, color='white', ec=BG, lw=2, zorder=12)
        ax.add_patch(pivot)

        # ── Score & rating text ───────────────────────────────────────
        ax.text(0, -0.18, str(int(round(score))),
                ha='center', va='center',
                fontsize=48, color='white', fontweight='bold', zorder=15)

        rating_color = RATING_COLORS.get(rating.lower().strip(), '#ECEFF1')
        ax.text(0, -0.35, rating.upper(),
                ha='center', va='center',
                fontsize=16, color=rating_color, fontweight='bold', zorder=15)

        # ── Header (Title & Combined Date/Time) ───────────────────────
        ax.text(0, 1.32, 'Fear & Greed Index',
                ha='center', va='center',
                fontsize=18, color='white', fontweight='bold')
        ax.text(0, 1.20, header_info,
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
