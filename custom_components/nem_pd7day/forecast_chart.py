"""
7-day forecast chart rendered from coordinator price data.

Pure computation — no HA dependencies, fully testable.

render_forecast_chart(forecast_data, region) -> bytes (PNG)

The chart shows:
  - Raw PD7day forecast line (grey, thin)
  - Calibrated forecast line (dark blue, bold) with confidence-tier styling (Rec 5)
  - p10/p90 confidence band (light blue shaded, opacity by horizon)
  - ToD background shading (shoulder/morning_ramp/solar/peak)
  - Horizon-gated spike callouts (Rec 1): suppressed beyond 48h
  - Spike persistence styling (Rec 4): confirmed vs candidate callouts
  - 24h/72h confidence boundary lines (Rec 5)
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# ToD background colours (very light fills)
_TOD_COLORS = {
    "shoulder": "#F5F5F5",
    "morning_ramp": "#FFF8E1",
    "solar": "#FFFDE7",
    "peak": "#FFF0F0",
}

# Y-axis clip for passthrough_high intervals
_PASSTHROUGH_CLIP = 2.0

# Spike callout thresholds (Rec 1) — imported from const at module level
# to keep the chart renderer self-contained for testing, these are also
# defined here as defaults and can be overridden by the data dict.
_SPIKE_CALLOUT_THRESHOLD_24H = 1.50  # $/kWh
_SPIKE_CALLOUT_THRESHOLD_48H = 3.00  # $/kWh


def _tod_label(hour: int) -> str:
    """Classify an hour into a time-of-day label."""
    if 6 <= hour < 9:
        return "morning_ramp"
    if 9 <= hour < 16:
        return "solar"
    if 16 <= hour < 21:
        return "peak"
    return "shoulder"


def _is_spike_callout_eligible(raw_value: float, horizon_hours: float, spike_first_run: bool) -> tuple[bool, str]:
    """Determine if an interval qualifies for spike callout display (Rec 1 + Rec 4).

    Returns (eligible, style) where style is one of:
      - "confirmed" — solid red callout (appeared in prior run too)
      - "candidate" — light grey callout (first run only)
      - "" — not eligible
    """
    # Rec 1: horizon gating — no callouts beyond 48h ever
    if horizon_hours >= 48:
        return False, ""
    # Rec 1: threshold depends on horizon
    if horizon_hours < 24:
        if raw_value < _SPIKE_CALLOUT_THRESHOLD_24H:
            return False, ""
    else:
        # 24-48h range
        if raw_value < _SPIKE_CALLOUT_THRESHOLD_48H:
            return False, ""
    # Rec 4: persistence check — first-run spikes are candidates, not confirmed
    if spike_first_run:
        return True, "candidate"
    return True, "confirmed"


def render_forecast_chart(forecast_data: list, region: str, annotations: list | None = None) -> bytes:
    """Render the 7-day forecast chart. Returns PNG bytes."""
    import datetime
    from collections import defaultdict
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt  # needed for Line2D legend proxies only
    import matplotlib.dates as mdates
    import matplotlib.figure as mplfig
    import matplotlib.patches as mpatches
    import matplotlib.ticker as ticker
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    NEM_TZ = datetime.timezone(datetime.timedelta(hours=10))

    if not forecast_data:
        return b""

    times, raws, cals, p10s, p90s, sources = [], [], [], [], [], []
    horizons = []
    spike_first_runs = []
    forecast_run_at = None
    for p in forecast_data:
        try:
            dt = datetime.datetime.fromisoformat(p['nemtime'])
            times.append(dt)
            raws.append(float(p.get('raw_value') or 0))
            cal_val = float(p.get('calibrated') or p.get('value') or 0)
            cals.append(cal_val)
            p10s.append(float(p.get('p10') if p.get('p10') is not None else cal_val))
            p90s.append(float(p.get('p90') if p.get('p90') is not None else cal_val))
            sources.append(p.get('calibrated_source', 'ols'))
            horizons.append(float(p.get('horizon_hours', 0)))
            spike_first_runs.append(p.get('spike_first_run', True))
            if forecast_run_at is None and p.get('forecast_run_at'):
                try:
                    forecast_run_at = datetime.datetime.fromisoformat(p['forecast_run_at'])
                except (ValueError, TypeError):
                    pass
        except (KeyError, ValueError):
            continue

    if not times:
        return b""

    times = np.array(times)
    raws  = np.array(raws)
    cals  = np.array(cals)
    p10s  = np.array(p10s)
    p90s  = np.array(p90s)

    # Dynamic clip: 99th percentile of calibrated values + 15% headroom, min 0.15
    # Exclude spike passthrough values from CLIP_Y to avoid compressing the chart
    non_spike_mask = np.array([s not in ('passthrough_high', 'covariate_capped') for s in sources])
    non_spike_cals = cals[non_spike_mask] if non_spike_mask.any() else cals
    p99 = float(np.percentile(non_spike_cals, 99)) if len(non_spike_cals) > 0 else 0.20
    CLIP_Y = float(np.ceil(max(p99 * 1.15, 0.15) / 0.05) * 0.05)

    # Per-day min/max on calibrated values (exclude spike passthroughs)
    by_day = defaultdict(list)
    for i, (t, s) in enumerate(zip(times, sources)):
        if s not in ('passthrough_high', 'covariate_capped'):
            by_day[t.strftime('%Y-%m-%d')].append((i, float(cals[i])))
    day_extremes = {}
    for day, pts in by_day.items():
        day_extremes[day] = {
            'min': min(pts, key=lambda x: x[1]),
            'max': max(pts, key=lambda x: x[1]),
        }

    fig = mplfig.Figure(figsize=(15, 6), facecolor='white')
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_facecolor('white')

    # ── Rec 5: Compute confidence zone boundaries ────────────────────────────
    # Zones relative to forecast_run_at:
    #   Zone A: 0-24h  — solid line, full band opacity
    #   Zone B: 24-72h — faded line, reduced band opacity
    #   Zone C: 72h+   — dotted line, very low band opacity
    zone_24h = None
    zone_72h = None
    if forecast_run_at is not None:
        zone_24h = forecast_run_at + datetime.timedelta(hours=24)
        zone_72h = forecast_run_at + datetime.timedelta(hours=72)

    # ── Grid stress annotations ──────────────────────────────────────────────
    notice_types_present: set[tuple] = set()
    if annotations:
        NOTICE_COLORS = {
            ("LOR", 1): ("#F39C12", 0.15, "LOR1"),   # amber
            ("LOR", 2): ("#E67E22", 0.20, "LOR2"),   # orange
            ("LOR", 3): ("#C0392B", 0.30, "LOR3"),   # red
            ("MSL", 1): ("#8E44AD", 0.15, "MSL1"),   # purple
            ("MSL", 2): ("#7D3C98", 0.22, "MSL2"),
            ("MSL", 3): ("#6C3483", 0.30, "MSL3"),
        }
        # Track label positions to stagger vertically when notices overlap in time
        # key: notice_id or index, value: y offset tier (0, 1, 2...)
        label_y_levels = [CLIP_Y * 1.28, CLIP_Y * 1.20, CLIP_Y * 1.12]
        # Group placed labels by approximate x-position bucket (6h windows)
        # to detect collisions and assign vertical tiers
        placed: list[tuple] = []  # (mid_num, tier)
        for ann in annotations:
            if ann.is_cancelled:
                continue
            color_info = NOTICE_COLORS.get((ann.notice_type, ann.level))
            if not color_info:
                continue
            color, alpha, label_text = color_info
            notice_types_present.add((ann.notice_type, ann.level))
            ax.axvspan(
                ann.period_from, ann.period_to,
                alpha=alpha, color=color, zorder=1, linewidth=0
            )
            mid = ann.period_from + (ann.period_to - ann.period_from) / 2
            mid_num = mdates.date2num(mid)
            # Assign vertical tier: find lowest tier not already used within
            # a 6-hour window of this label's mid-point
            tier = 0
            for _ in range(len(label_y_levels)):
                collision = any(
                    abs(mid_num - px) < 0.25 and pt == tier
                    for px, pt in placed
                )
                if not collision:
                    break
                tier += 1
            tier = min(tier, len(label_y_levels) - 1)
            placed.append((mid_num, tier))
            ax.text(
                mid, label_y_levels[tier], label_text,
                ha="center", va="top", fontsize=7, color=color,
                fontweight="bold", zorder=5,
            )

    # ── Rec 5: Confidence-tiered p10/p90 band and forecast lines ─────────────
    # Split data into three zones and render each with different styling.
    if zone_24h is not None:
        zone_a = np.array([t < zone_24h for t in times])
        zone_b = np.array([zone_24h <= t < zone_72h for t in times])
        zone_c = np.array([t >= zone_72h for t in times])

        # Extend each zone by 1 point at the boundary for seamless joins
        for z_prev, z_next in [(zone_a, zone_b), (zone_b, zone_c)]:
            idx_prev = np.where(z_prev)[0]
            idx_next = np.where(z_next)[0]
            if len(idx_prev) > 0 and len(idx_next) > 0:
                z_next[idx_prev[-1]] = True  # overlap last point of prev zone

        # Zone A (0-24h): solid line, full band
        if zone_a.any():
            ax.fill_between(times[zone_a],
                            np.clip(p10s[zone_a], None, CLIP_Y),
                            np.clip(p90s[zone_a], None, CLIP_Y),
                            color='#BBDEFB', alpha=0.45, zorder=2)
            ax.plot(times[zone_a], np.clip(cals[zone_a], None, CLIP_Y),
                    color='#1565C0', linewidth=2.0, zorder=4)
            ax.plot(times[zone_a], np.clip(raws[zone_a], None, CLIP_Y),
                    color='#888888', linewidth=1.0, alpha=0.7, linestyle='--', zorder=5)

        # Zone B (24-72h): faded line, reduced band
        if zone_b.any():
            ax.fill_between(times[zone_b],
                            np.clip(p10s[zone_b], None, CLIP_Y),
                            np.clip(p90s[zone_b], None, CLIP_Y),
                            color='#BBDEFB', alpha=0.12, zorder=2,
                            hatch='///', edgecolor='#90CAF9', linewidth=0.0)
            ax.plot(times[zone_b], np.clip(cals[zone_b], None, CLIP_Y),
                    color='#1565C0', linewidth=1.6, alpha=0.65, zorder=4)
            ax.plot(times[zone_b], np.clip(raws[zone_b], None, CLIP_Y),
                    color='#888888', linewidth=0.8, alpha=0.5, linestyle='--', zorder=5)

        # Zone C (72h+): dotted line, very low band
        if zone_c.any():
            ax.fill_between(times[zone_c],
                            np.clip(p10s[zone_c], None, CLIP_Y),
                            np.clip(p90s[zone_c], None, CLIP_Y),
                            color='#BBDEFB', alpha=0.06, zorder=2)
            ax.plot(times[zone_c], np.clip(cals[zone_c], None, CLIP_Y),
                    color='#1565C0', linewidth=1.2, alpha=0.45, linestyle=':', zorder=4)
            ax.plot(times[zone_c], np.clip(raws[zone_c], None, CLIP_Y),
                    color='#888888', linewidth=0.7, alpha=0.35, linestyle=':', zorder=5)

        # 24h and 72h boundary lines
        y_min_prelim = min(float(np.min(p10s)), -0.04)
        y_top_prelim = CLIP_Y * 1.35
        if times[0] < zone_24h < times[-1]:
            ax.axvline(zone_24h, color='#666688', linewidth=0.8,
                       linestyle='--', alpha=0.3, zorder=3)
            ax.text(zone_24h, y_top_prelim * 0.96,
                    '\u2190 reliable | uncertain \u2192',
                    fontsize=6.5, color='#666688', alpha=0.6,
                    ha='center', va='top', zorder=8)
        if times[0] < zone_72h < times[-1]:
            ax.axvline(zone_72h, color='#666688', linewidth=0.8,
                       linestyle='--', alpha=0.3, zorder=3)
    else:
        # No forecast_run_at: render uniformly (legacy path)
        ax.fill_between(times, np.clip(p10s, None, CLIP_Y), np.clip(p90s, None, CLIP_Y),
                        color='#BBDEFB', alpha=0.45, zorder=2)
        ax.plot(times, np.clip(cals, None, CLIP_Y),
                color='#1565C0', linewidth=2.0, label='Calibrated', zorder=4)
        ax.plot(times, np.clip(raws, None, CLIP_Y),
                color='#888888', linewidth=1.0, alpha=0.7, linestyle='--',
                label='PD7day Raw', zorder=5)

    # Per-day min/max labels
    for day, ex in sorted(day_extremes.items()):
        mi, mv = ex['max']
        mt = times[mi]
        mv_plot = min(mv, CLIP_Y)
        ax.scatter([mt], [mv_plot], color='#C62828', s=30, zorder=7, marker='o', linewidths=0)
        ax.annotate(f'${mv:.3f}', xy=(mt, mv_plot),
                    xytext=(0, 9), textcoords='offset points',
                    fontsize=7.2, color='#B71C1C', ha='center', va='bottom',
                    fontweight='semibold', zorder=9)

        ni, nv = ex['min']
        nt = times[ni]
        ax.scatter([nt], [nv], color='#1B5E20', s=30, zorder=7, marker='o', linewidths=0)
        ax.annotate(f'${nv:.3f}', xy=(nt, nv),
                    xytext=(0, -11), textcoords='offset points',
                    fontsize=7.2, color='#1B5E20', ha='center', va='top',
                    fontweight='semibold', zorder=9)

    # ── Rec 1 + 4: Horizon-gated spike callouts with persistence styling ─────
    # Classify each passthrough_high interval using horizon + persistence rules.
    confirmed_indices = []
    candidate_indices = []
    for i, s in enumerate(sources):
        if s != 'passthrough_high':
            continue
        eligible, style = _is_spike_callout_eligible(
            float(raws[i]), horizons[i], spike_first_runs[i],
        )
        if not eligible:
            continue
        if style == "confirmed":
            confirmed_indices.append(i)
        elif style == "candidate":
            candidate_indices.append(i)

    # Confirmed spike markers — solid red triangle (existing style)
    if confirmed_indices:
        ct = [times[i] for i in confirmed_indices]
        ax.scatter(ct, [CLIP_Y * 0.96] * len(ct),
                   color='#C62828', marker='^', s=55, zorder=6)

    # Candidate spike markers — light grey triangle (first-run, unconfirmed)
    if candidate_indices:
        ct = [times[i] for i in candidate_indices]
        ax.scatter(ct, [CLIP_Y * 0.96] * len(ct),
                   color='#AAAAAA', marker='^', s=35, zorder=6, alpha=0.6)

    # Build callout clusters from confirmed spikes only
    pt_indices = confirmed_indices
    if pt_indices:
        clusters: list[list[int]] = []
        current: list[int] = [pt_indices[0]]
        for prev_idx, idx in zip(pt_indices, pt_indices[1:]):
            gap = (times[idx] - times[prev_idx]).total_seconds() / 60
            if gap <= 60:
                current.append(idx)
            else:
                clusters.append(current)
                current = [idx]
        clusters.append(current)

        chart_start = times[0]
        chart_end = times[-1]
        chart_span = (chart_end - chart_start).total_seconds()
        y_offsets = [45, 65, 45, 65]
        for cluster_num, cluster in enumerate(clusters):
            c_times = [times[i] for i in cluster]
            c_vals = [float(cals[i]) for i in cluster]
            max_val = max(c_vals)
            peak_idx = c_vals.index(max_val)
            peak_time = c_times[peak_idx]
            frac = (peak_time - chart_start).total_seconds() / chart_span if chart_span > 0 else 0.5
            xoff = 32 if frac < 0.5 else -32
            yoff = y_offsets[cluster_num % len(y_offsets)]
            ha = 'left' if xoff > 0 else 'right'
            ax.annotate(
                f'${max_val:.2f}/kWh',
                xy=(peak_time, CLIP_Y),
                xytext=(xoff, yoff), textcoords='offset points',
                fontsize=7.5, color='#C62828', ha=ha, va='bottom',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                          edgecolor='#C62828', alpha=0.9),
                arrowprops=dict(arrowstyle='->', color='#C62828', lw=1.2), zorder=10)

    # Grid
    ax.yaxis.grid(True, color='#DDDDDD', linewidth=0.5, alpha=0.7, zorder=1)
    ax.xaxis.grid(True, color='#EEEEEE', linewidth=0.4, alpha=0.5, zorder=1)
    ax.set_axisbelow(True)

    # X-axis: minor ticks every 6h (grid only), labelled ticks at 06:00 and 18:00 only
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[6, 18], tz=NEM_TZ))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=NEM_TZ))
    ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[0, 12], tz=NEM_TZ))
    ax.xaxis.grid(True, which='minor', color='#EEEEEE', linewidth=0.4, alpha=0.5, zorder=1)
    ax.tick_params(axis='x', labelsize=8.5, pad=2)

    y_min = min(float(np.min(p10s)), -0.04)
    y_top = CLIP_Y * 1.35
    for mt in [t for t in times if t.hour == 0 and t.minute == 0]:
        ax.axvline(mdates.date2num(mt), color='#CCCCCC', linewidth=1.0,
                   linestyle='--', zorder=1)
        ax.text(mdates.date2num(mt) + 0.02, y_min * 0.97,
                mt.strftime('%a %-d %b'), fontsize=9, color='#333333',
                va='top', ha='left', zorder=8,
                bbox=dict(facecolor='white', edgecolor='none', alpha=0.7, pad=1))

    # Y-axis left: $/kWh
    ax.set_ylim(bottom=y_min * 1.25, top=y_top)
    ax.set_ylabel('$/kWh', fontsize=10, labelpad=6)
    ax.yaxis.set_tick_params(labelsize=9)
    ax.axhline(CLIP_Y, color='#C62828', linewidth=0.8, linestyle=':', alpha=0.6)
    ax.text(times[min(2, len(times) - 1)], CLIP_Y * 1.02,
            f'clip: p99+15% = ${CLIP_Y:.2f}/kWh',
            fontsize=7, color='#C62828', va='bottom', alpha=0.85)

    # Y-axis right: $/MWh — use twinx on the figure's ax (OO API, thread-safe)
    ax2 = ax.twinx()
    ax2.set_ylim(ax.get_ylim()[0] * 1000, ax.get_ylim()[1] * 1000)
    ax2.set_ylabel('$/MWh', fontsize=10, labelpad=8)
    ax2.yaxis.set_tick_params(labelsize=9, pad=4)
    ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f'{x:.0f}'))

    # Title
    ax.set_title(f'NEM PD7DAY {region} \u2014 7-Day Pre-Dispatch Spot Price Forecast',
                 fontsize=13, fontweight='bold', pad=11, color='#1A1A1A')

    # Legend
    line_legend = [
        plt.Line2D([0], [0], color='#888888', linewidth=1.0, linestyle='--', alpha=0.7, label='PD7day Raw'),
        plt.Line2D([0], [0], color='#1565C0', linewidth=2.5, label='Calibrated (0\u201324h)'),
        plt.Line2D([0], [0], color='#1565C0', linewidth=1.6, alpha=0.65, label='Calibrated (24\u201372h)'),
        plt.Line2D([0], [0], color='#1565C0', linewidth=1.2, alpha=0.45, linestyle=':', label='Calibrated (72h+)'),
        mpatches.Patch(color='#BBDEFB', alpha=0.6, label='p10\u2013p90 band'),
        plt.Line2D([0], [0], marker='o', color='w', markerfacecolor='#C62828',
                   markersize=6, label='Daily max'),
        plt.Line2D([0], [0], marker='o', color='w', markerfacecolor='#1B5E20',
                   markersize=6, label='Daily min'),
    ]
    if confirmed_indices:
        line_legend.append(plt.Line2D([0], [0], marker='^', color='w',
                                      markerfacecolor='#C62828', markersize=8,
                                      label='Confirmed Spike'))
    if candidate_indices:
        line_legend.append(plt.Line2D([0], [0], marker='^', color='w',
                                      markerfacecolor='#AAAAAA', markersize=6,
                                      label='Candidate Spike (1 run)'))
    if zone_24h is not None:
        line_legend.append(plt.Line2D([0], [0], color='#666688', linewidth=0.8,
                                      linestyle='--', alpha=0.3,
                                      label='24h confidence boundary'))
    # Add legend entries for any notice types actually present in this chart
    NOTICE_LEGEND = {
        ("LOR", 1): ("#F39C12", "LOR1 \u2014 Reserve notice"),
        ("LOR", 2): ("#E67E22", "LOR2 \u2014 Reserve notice"),
        ("LOR", 3): ("#C0392B", "LOR3 \u2014 Reserve notice"),
        ("MSL", 1): ("#8E44AD", "MSL1 \u2014 Min load notice"),
        ("MSL", 2): ("#7D3C98", "MSL2 \u2014 Min load notice"),
        ("MSL", 3): ("#6C3483", "MSL3 \u2014 Min load notice"),
    }
    for key in sorted(notice_types_present):
        if key in NOTICE_LEGEND:
            col, lbl = NOTICE_LEGEND[key]
            line_legend.append(mpatches.Patch(color=col, alpha=0.5, label=lbl))
    ax.legend(handles=line_legend, loc='upper right', fontsize=8.5,
              framealpha=0.92, edgecolor='#CCCCCC', borderpad=0.7)

    ax.set_xlim(times[0], times[-1] + datetime.timedelta(minutes=30))
    fig.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=110, bbox_inches='tight', facecolor='white')
    buf.seek(0)
    return buf.getvalue()
