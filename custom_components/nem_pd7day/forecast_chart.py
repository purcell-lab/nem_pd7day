"""
7-day forecast chart rendered from coordinator price data.

Pure computation — no HA dependencies, fully testable.

render_forecast_chart(forecast_data, region) -> bytes (PNG)

The chart shows:
  - Raw PD7day forecast line (grey, thin)
  - Calibrated forecast line (dark blue, bold)
  - p10/p90 confidence band (light blue shaded)
  - ToD background shading (shoulder/morning_ramp/solar/peak)
  - passthrough_high annotations (clipped at 2.0 $/kWh)
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


def _tod_label(hour: int) -> str:
    """Classify an hour into a time-of-day label."""
    if 6 <= hour < 9:
        return "morning_ramp"
    if 9 <= hour < 16:
        return "solar"
    if 16 <= hour < 21:
        return "peak"
    return "shoulder"


def render_forecast_chart(forecast_data: list, region: str) -> bytes:
    """
    Render the 7-day forecast chart as PNG bytes.

    Args:
        forecast_data: list of forecast interval dicts, each with:
            nemtime, raw_value, calibrated, p10, p90, calibrated_source,
            horizon_hours
        region: NEM region string e.g. "QLD1"

    Returns:
        PNG image bytes, or b"" if no data or matplotlib unavailable.
    """
    if not forecast_data:
        return b""

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        _LOGGER.warning("forecast_chart: matplotlib not available")
        return b""

    # Parse data
    times: list[datetime] = []
    raw_vals: list[float] = []
    cal_vals: list[float] = []
    p10_vals: list[float] = []
    p90_vals: list[float] = []
    sources: list[str] = []

    for interval in forecast_data:
        try:
            t = datetime.fromisoformat(interval["nemtime"])
        except (ValueError, KeyError, TypeError):
            continue

        raw = interval.get("raw_value")
        if raw is None:
            continue

        times.append(t)
        raw_vals.append(float(raw))
        cal_vals.append(float(interval.get("calibrated", raw)))
        p10_vals.append(float(interval.get("p10", raw)))
        p90_vals.append(float(interval.get("p90", raw)))
        sources.append(interval.get("calibrated_source", ""))

    if not times:
        return b""

    # Figure setup
    fig, ax = plt.subplots(figsize=(12, 5), facecolor="#FFFFFF")
    ax.set_facecolor("#FFFFFF")

    # ToD background shading — draw vertical spans for contiguous blocks
    _draw_tod_shading(ax, times)

    # p10/p90 confidence band
    ax.fill_between(
        times, p10_vals, p90_vals,
        color="#BBDEFB", alpha=0.3, label="p10\u2013p90", zorder=2,
    )

    # Raw forecast line
    ax.plot(
        times, raw_vals,
        color="#AAAAAA", linewidth=1, alpha=0.7, label="PD7day Raw", zorder=3,
    )

    # Calibrated forecast line
    ax.plot(
        times, cal_vals,
        color="#1565C0", linewidth=2, label="Calibrated", zorder=4,
    )

    # passthrough_high handling — annotate clipped values
    _draw_passthrough_annotations(ax, times, cal_vals, sources)

    # Y-axis scaling
    _set_y_limits(ax, cal_vals, p10_vals, p90_vals, sources)

    # X-axis formatting — NEM datetime labels
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %-d %b"))
    ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[0, 6, 12, 18]))
    ax.xaxis.set_minor_formatter(mdates.DateFormatter("%H:%M"))
    ax.tick_params(axis="x", which="major", labelsize=9, pad=15, rotation=0)
    ax.tick_params(axis="x", which="minor", labelsize=7, rotation=0)

    # Labels and title
    ax.set_ylabel("$/kWh", fontsize=11, color="#444444")
    ax.set_title(
        f"NEM PD7DAY {region} 7-Day Forecast",
        fontsize=13, fontweight="bold", color="#111111", pad=10,
    )

    # Grid
    ax.grid(axis="y", color="#DDDDDD", linewidth=0.7, alpha=0.3, zorder=0)
    ax.grid(axis="x", which="minor", color="#EEEEEE", linewidth=0.5, alpha=0.3, zorder=0)

    # Legend
    ax.legend(loc="upper left", fontsize=9, facecolor="#FFFFFF", edgecolor="#CCCCCC",
              framealpha=0.95)

    # Spine styling
    for sp in ax.spines.values():
        sp.set_edgecolor("#CCCCCC")

    fig.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor="#FFFFFF")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _draw_tod_shading(ax, times: list[datetime]) -> None:
    """Draw vertical spans for contiguous blocks of the same ToD label."""
    if not times:
        return

    current_label = _tod_label(times[0].hour)
    block_start = times[0]

    for i in range(1, len(times)):
        label = _tod_label(times[i].hour)
        if label != current_label:
            color = _TOD_COLORS.get(current_label)
            if color:
                ax.axvspan(block_start, times[i], color=color, alpha=1.0, zorder=0)
            current_label = label
            block_start = times[i]

    # Final block
    color = _TOD_COLORS.get(current_label)
    if color:
        ax.axvspan(block_start, times[-1], color=color, alpha=1.0, zorder=0)


def _draw_passthrough_annotations(
    ax, times: list[datetime], cal_vals: list[float], sources: list[str],
) -> None:
    """Mark passthrough_high intervals with red dots and value annotations."""
    for i, src in enumerate(sources):
        if src == "passthrough_high" and cal_vals[i] > _PASSTHROUGH_CLIP:
            actual_val = cal_vals[i]
            ax.plot(
                times[i], _PASSTHROUGH_CLIP,
                marker="v", color="#D32F2F", markersize=8, zorder=6,
            )
            ax.annotate(
                f"${actual_val:.2f}",
                xy=(times[i], _PASSTHROUGH_CLIP),
                xytext=(0, 8), textcoords="offset points",
                fontsize=7, color="#D32F2F", fontweight="bold",
                ha="center", va="bottom",
            )


def _set_y_limits(
    ax,
    cal_vals: list[float],
    p10_vals: list[float],
    p90_vals: list[float],
    sources: list[str],
) -> None:
    """Set Y-axis limits, clipping at 2.0 $/kWh if passthrough_high present."""
    has_passthrough = any(s == "passthrough_high" for s in sources)

    # Filter out passthrough_high values for normal scaling
    normal_cal = [v for v, s in zip(cal_vals, sources) if s != "passthrough_high"]
    normal_p90 = [v for v, s in zip(p90_vals, sources) if s != "passthrough_high"]

    if has_passthrough:
        y_max = _PASSTHROUGH_CLIP
    else:
        max_vals = normal_cal + normal_p90 if normal_cal else cal_vals + p90_vals
        y_max = max(max_vals) * 1.2 if max_vals else 1.0

    y_min = min(min(p10_vals), 0) if p10_vals else 0
    if y_min == y_max:
        y_max = y_min + 0.1
    ax.set_ylim(y_min, y_max)
