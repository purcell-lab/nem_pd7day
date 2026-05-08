"""
Isotonic calibration goodness dashboard.

render_iso_chart(calibration_result, iso_history, obs_count, region) -> bytes (PNG)

Four panels:
  A (top, full width)   — Heatmap: compression_ratio per horizon x ToD bucket
  B (middle-left)       — Bar chart: iso_mae per bucket (top 16, sorted by horizon)
  C (middle-right)      — Scatter: n_steps vs n (bubble size = iso_mae)
  D (bottom, full width)— Time-series: compression_ratio drift over recent fit cycles
                          for key buckets (h00_06__solar, h24_48__peak, h96plus__peak)
"""
from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .calibration_engine import CalibrationResult

_LOGGER = logging.getLogger(__name__)

# Chart-level constants
MIN_OBS = 20  # Minimum observations for a bucket to be considered fitted in chart

_HORIZONS = ["h00_06", "h06_12", "h12_24", "h24_48", "h48_96", "h96plus"]
_HOR_LABELS = [
    "0-6 h", "6-12 h", "12-24 h", "24-48 h", "48-96 h", "96+ h"
]
_TODS = ["shoulder", "morning_ramp", "solar", "peak"]
_TOD_LABELS = ["Shoulder", "Morning Ramp", "Solar", "Peak"]
_TOD_COL = {
    "shoulder": "#7D3C98",
    "morning_ramp": "#E67E22",
    "solar": "#D4860A",
    "peak": "#C0392B",
}

# Palette
_BG = "#F8F9FA"
_PAN = "#FFFFFF"
_GRD = "#DEE2E6"

# Key buckets for time-series panel D
_KEY_BUCKETS = {
    "h00_06__solar": ("blue", "h00_06 solar"),
    "h24_48__peak": ("red", "h24_48 peak"),
    "h96plus__peak": ("purple", "h96plus peak"),
}


def render_iso_chart(
    calibration_result: CalibrationResult | None,
    iso_history: list[dict] | None = None,
    obs_count: int = 0,
    region: str = "QLD1",
) -> bytes:
    """Render the 4-panel isotonic calibration goodness dashboard as PNG bytes."""
    if calibration_result is None:
        return b""

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    from matplotlib.colors import TwoSlopeNorm

    summary = calibration_result.summary()
    buckets = summary["buckets"]
    fitted_str = summary.get("fitted_at", "unknown")

    if iso_history is None:
        iso_history = []

    fig = plt.figure(figsize=(18, 20), dpi=150, facecolor=_BG)
    gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.35, wspace=0.28)

    fig.suptitle(
        f"NEM PD7DAY \u00b7 {region} Isotonic Calibration Goodness  \u00b7  "
        f"{obs_count} obs  \u00b7  {fitted_str}",
        fontsize=14, fontweight="bold", y=0.98,
    )

    # ── Panel A: Compression Ratio Heatmap (top, full width) ─────────────────
    ax_a = fig.add_subplot(gs[0, :])
    _render_panel_a(ax_a, buckets)

    # ── Panel B: iso_mae bar chart (middle-left) ─────────────────────────────
    ax_b = fig.add_subplot(gs[1, 0])
    _render_panel_b(ax_b, buckets)

    # ── Panel C: n_steps scatter (middle-right) ──────────────────────────────
    ax_c = fig.add_subplot(gs[1, 1])
    _render_panel_c(ax_c, buckets)

    # ── Panel D: compression_ratio drift time-series (bottom, full width) ────
    ax_d = fig.add_subplot(gs[2, :])
    _render_panel_d(ax_d, iso_history)

    # Footer
    fig.text(
        0.5, 0.01,
        f"Source: AEMO PD7DAY \u00b7 github.com/purcell-lab/nem_pd7day \u00b7 Fitted: {fitted_str}",
        ha="center", fontsize=8, color="#6C757D",
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _render_panel_a(ax, buckets: dict) -> None:
    """Panel A — Compression Ratio Heatmap."""
    from matplotlib.colors import TwoSlopeNorm

    n_rows = len(_HORIZONS)
    n_cols = len(_TODS)
    grid = np.full((n_rows, n_cols), np.nan)
    n_grid = np.zeros((n_rows, n_cols), dtype=int)

    for r, hor in enumerate(_HORIZONS):
        for c, tod in enumerate(_TODS):
            key = f"{hor}__{tod}"
            b = buckets.get(key)
            if b is not None:
                n_grid[r, c] = b["n"]
                cr = b.get("compression_ratio")
                if cr is not None and b["n"] >= MIN_OBS:
                    grid[r, c] = cr

    norm = TwoSlopeNorm(vmin=0.3, vcenter=1.0, vmax=1.5)
    im = ax.imshow(
        grid, cmap="RdBu_r", norm=norm, aspect="auto",
        interpolation="nearest",
    )

    ax.set_xticks(range(n_cols))
    ax.set_xticklabels(_TOD_LABELS, fontsize=9)
    ax.set_yticks(range(n_rows))
    ax.set_yticklabels(_HOR_LABELS, fontsize=9)

    # Annotate each cell
    for r in range(n_rows):
        for c in range(n_cols):
            n = n_grid[r, c]
            cr = grid[r, c]
            if n < MIN_OBS:
                ax.text(c, r, f"n={n}\n< min obs", ha="center", va="center",
                        fontsize=7, color="#999999")
            elif np.isnan(cr):
                ax.text(c, r, f"n={n}", ha="center", va="center",
                        fontsize=7, color="#999999")
            else:
                ax.text(c, r, f"cr={cr:.2f}\nn={n}", ha="center", va="center",
                        fontsize=8, fontweight="bold",
                        color="white" if abs(cr - 1.0) > 0.25 else "black")

    ax.set_title(
        "Compression Ratio (y_range / x_range) \u2014 <1 = AEMO over-forecasts, =1 = no correction",
        fontsize=11, pad=10,
    )
    ax.set_facecolor(_PAN)

    cbar = ax.figure.colorbar(im, ax=ax, fraction=0.03, pad=0.04)
    cbar.set_label("compression_ratio", fontsize=8)


def _render_panel_b(ax, buckets: dict) -> None:
    """Panel B — iso_mae bar chart (all 24 buckets, sorted by horizon then ToD)."""
    ordered_keys = [f"{hor}__{tod}" for hor in _HORIZONS for tod in _TODS]
    labels = []
    values = []
    colors = []
    is_grey = []

    for key in ordered_keys:
        b = buckets.get(key, {"n": 0, "iso_mae": None})
        n = b.get("n", 0)
        mae = b.get("iso_mae")
        tod = key.split("__")[-1] if "__" in key else "shoulder"
        labels.append(key.replace("__", "\n"))

        if n < MIN_OBS or mae is None:
            values.append(0)
            colors.append("#CCCCCC")
            is_grey.append(True)
        else:
            values.append(mae)
            colors.append(_TOD_COL.get(tod, "#999999"))
            is_grey.append(False)

    x = np.arange(len(labels))
    bars = ax.bar(x, values, color=colors, edgecolor="white", linewidth=0.5)

    # Label grey bars
    for i, grey in enumerate(is_grey):
        if grey:
            ax.text(i, 0.001, "< min\nobs", ha="center", va="bottom",
                    fontsize=5, color="#999999")

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=5, rotation=90)
    ax.set_ylabel("Mean Calibration Shift ($/kWh)", fontsize=9)
    ax.set_title("Mean Absolute Calibration Shift per Bucket", fontsize=11, pad=10)
    ax.set_facecolor(_PAN)
    ax.grid(axis="y", color=_GRD, linewidth=0.5)


def _render_panel_c(ax, buckets: dict) -> None:
    """Panel C — n_steps scatter (bubble size = iso_mae)."""
    xs, ys, sizes, colors_list, annot = [], [], [], [], []

    for key, b in buckets.items():
        n = b.get("n", 0)
        n_steps = b.get("iso_n_steps")
        mae = b.get("iso_mae")
        if n_steps is None or n < MIN_OBS:
            continue
        tod = key.split("__")[-1] if "__" in key else "shoulder"
        xs.append(n)
        ys.append(n_steps)
        # Scale: typical 0.02 mae = marker size 80
        s = (mae / 0.02) * 80 if mae is not None and mae > 0 else 20
        sizes.append(s)
        colors_list.append(_TOD_COL.get(tod, "#999999"))
        annot.append((key, n_steps))

    if xs:
        ax.scatter(xs, ys, s=sizes, c=colors_list, alpha=0.7, edgecolors="white", linewidth=0.5)

        # Reference line: expected n_steps ~ sqrt(n)
        x_ref = np.linspace(1, max(xs) * 1.1, 50)
        ax.plot(x_ref, np.sqrt(x_ref), "--", color="#AAAAAA", linewidth=1, label=r"$\sqrt{n}$ reference")
        ax.legend(fontsize=7, loc="upper left")

        # Annotate top-5 by n_steps
        top5 = sorted(annot, key=lambda t: t[1], reverse=True)[:5]
        top5_keys = {t[0] for t in top5}
        for i, (key, _) in enumerate(annot):
            if key in top5_keys:
                ax.annotate(
                    key.replace("__", "\n"), (xs[i], ys[i]),
                    fontsize=5, ha="left", va="bottom",
                    xytext=(4, 4), textcoords="offset points",
                )

    ax.set_xlabel("n (training count)", fontsize=9)
    ax.set_ylabel("n_steps (PAV blocks)", fontsize=9)
    ax.set_title("PAV Complexity (n_steps) vs Training Count", fontsize=11, pad=10)
    ax.set_facecolor(_PAN)
    ax.grid(color=_GRD, linewidth=0.5)


def _render_panel_d(ax, iso_history: list[dict]) -> None:
    """Panel D — compression_ratio drift time-series for key buckets."""
    from datetime import datetime

    if len(iso_history) < 2:
        ax.text(
            0.5, 0.5, "Accumulating fit history\u2026",
            ha="center", va="center", fontsize=14, color="#999999",
            transform=ax.transAxes,
        )
        ax.set_facecolor(_PAN)
        ax.set_title("Compression Ratio Drift \u2014 Key Buckets", fontsize=11, pad=10)
        return

    for bucket_key, (color, label) in _KEY_BUCKETS.items():
        times = []
        vals = []
        for record in iso_history:
            cr = record.get("buckets", {}).get(bucket_key)
            if cr is not None:
                try:
                    ts = datetime.fromisoformat(record["fitted_at"])
                    times.append(ts)
                    vals.append(cr)
                except (ValueError, KeyError):
                    pass
        if times:
            ax.plot(times, vals, "-o", color=color, label=label,
                    markersize=4, linewidth=1.5)

    ax.axhline(1.0, color="#AAAAAA", linestyle="--", linewidth=1, label="no correction")
    ax.legend(fontsize=8, loc="upper left")

    import matplotlib.dates as mdates
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    ax.tick_params(axis="x", rotation=30, labelsize=8)

    ax.set_ylabel("compression_ratio", fontsize=9)
    ax.set_title("Compression Ratio Drift \u2014 Key Buckets", fontsize=11, pad=10)
    ax.set_facecolor(_PAN)
    ax.grid(color=_GRD, linewidth=0.5)
