"""
Duck-curve bias chart rendered from live calibration coefficients.

Pure computation — no HA dependencies, fully testable.

render_chart(calibration_result) -> bytes (PNG)

The chart has three panels:
  A  Duck curve (stylised) — actual vs AEMO raw vs calibrated
  B  Heatmap of OLS slope a per horizon × time-of-day bucket (live data)
  C  Bar chart of key bias patterns (live data, top-N buckets by |a - 1|)
"""
from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .calibration_engine import CalibrationResult

_LOGGER = logging.getLogger(__name__)

# Horizons and ToDs shown in heatmap (h48_96+ excluded — too sparse early on)
_HORIZONS   = ["h00_06", "h06_12", "h12_24", "h24_48"]
_HOR_LABELS = ["0–6h ahead", "6–12h ahead", "12–24h ahead", "24–48h ahead"]
_TODS       = ["solar", "peak", "shoulder", "offpeak"]
_TOD_LABELS = ["Solar\n10–16h", "Peak\n16–20h", "Shoulder\n20–22h", "Offpeak"]
_TOD_COL    = {
    "solar":    "#D4860A",
    "peak":     "#C0392B",
    "shoulder": "#7D3C98",
    "offpeak":  "#1A6EA8",
}

# Palette
_BG  = "#F8F9FA"
_PAN = "#FFFFFF"
_GRD = "#DEE2E6"


def _bucket_data(calibration_result: "CalibrationResult") -> dict[tuple[str, str], tuple[int, float, float]]:
    """
    Extract (n, a, b) for each (horizon, tod) cell from a CalibrationResult.

    Falls back to (0, 1.0, 0.0) for buckets below MIN_OBS.
    """
    data: dict[tuple[str, str], tuple[int, float, float]] = {}
    models = calibration_result.models if calibration_result else {}
    for hor in _HORIZONS:
        for tod in _TODS:
            key = f"{hor}__{tod}"
            bm = models.get(key)
            if bm is not None and bm.ols is not None and bm.ols.n > 0:
                data[(hor, tod)] = (bm.ols.n, bm.ols.a, bm.ols.b)
            else:
                data[(hor, tod)] = (0, 1.0, 0.0)
    return data


def render_chart(calibration_result: "CalibrationResult | None", obs_count: int = 0, region: str = "QLD1") -> bytes:
    """
    Render the duck-curve bias chart as PNG bytes.

    Parameters
    ----------
    calibration_result : CalibrationResult | None
        Live calibration from the coordinator.  If None, returns b"".
    obs_count : int
        Total observation count for the subtitle.
    region : str
        Region label for titles.

    Returns
    -------
    bytes  PNG image, or b"" if matplotlib unavailable or no calibration data.
    """
    if calibration_result is None:
        return b""

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        import numpy as np
        from matplotlib.colors import TwoSlopeNorm
    except ImportError:
        _LOGGER.warning("bias_chart: matplotlib not available")
        return b""

    data = _bucket_data(calibration_result)
    fitted_at = getattr(calibration_result, "fitted_at", "")
    fitted_str = fitted_at[:16].replace("T", " ") if fitted_at else ""

    # ── Figure layout ─────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(18, 16), facecolor=_BG)
    gs  = gridspec.GridSpec(
        2, 2, figure=fig,
        hspace=0.52, wspace=0.32,
        left=0.07, right=0.97,
        top=0.93, bottom=0.08,
    )

    # ─── Panel A: Duck curve (spans full top row) ─────────────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    ax1.set_facecolor(_PAN)

    h = np.linspace(0, 24, 500)

    def _actual(h):
        base = 0.068
        return (base
                + 0.008 * np.sin(np.pi * h / 24)
                - 0.033 * np.exp(-0.5 * ((h - 13) / 2.4) ** 2)
                + 0.120 * np.exp(-0.5 * ((h - 18.5) / 1.4) ** 2)
                + 0.028 * np.exp(-0.5 * ((h - 7.5) / 1.1) ** 2))

    def _aemo(h):
        base = 0.068
        return (base
                + 0.009 * np.sin(np.pi * h / 24)
                - 0.016 * np.exp(-0.5 * ((h - 13) / 2.4) ** 2)
                + 0.200 * np.exp(-0.5 * ((h - 18.5) / 1.4) ** 2)
                + 0.042 * np.exp(-0.5 * ((h - 7.5) / 1.1) ** 2))

    def _calib(h):
        raw = _aemo(h)
        pk  = 0.200 * np.exp(-0.5 * ((h - 18.5) / 1.4) ** 2)
        return raw - 0.55 * pk + 0.008 * np.exp(-0.5 * ((h - 13) / 2.4) ** 2)

    act_y  = _actual(h)
    aemo_y = _aemo(h)
    cal_y  = _calib(h)

    for start, end, tod in [(10, 16, "solar"), (16, 20, "peak"), (20, 22, "shoulder")]:
        ax1.axvspan(start, end, alpha=0.10, color=_TOD_COL[tod], zorder=1)

    ax1.fill_between(h, act_y,  alpha=0.15, color="#0A7C6E", zorder=2)
    ax1.fill_between(h, aemo_y, alpha=0.08, color="#C0392B", zorder=2)

    l1, = ax1.plot(h, act_y,  color="#0A7C6E", lw=2.5, label="Actual price",        zorder=5)
    l2, = ax1.plot(h, aemo_y, color="#C0392B", lw=2.0, label="AEMO PD7DAY (raw)",   zorder=4, ls="--")
    l3, = ax1.plot(h, cal_y,  color="#B7770D", lw=2.0, label="Calibrated forecast", zorder=4, ls="-.")

    for start, end, tod, lbl in [
        (10, 16, "solar",    "Solar 10–16h"),
        (16, 20, "peak",     "Peak 16–20h"),
        (20, 22, "shoulder", "Shoulder 20–22h"),
    ]:
        ax1.text((start + end) / 2, 0.262, lbl, ha="center", va="bottom",
                 fontsize=10, color=_TOD_COL[tod], fontweight="bold")

    ax1.annotate(
        "Over-forecast (a≈0.36)",
        xy=(18.5, 0.188), xytext=(21.0, 0.235),
        fontsize=10, color="#C0392B", fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#C0392B", lw=1.5),
        bbox=dict(boxstyle="round,pad=0.4", fc="#FEE8E8", ec="#C0392B", alpha=0.95),
    )
    ax1.annotate(
        "Solar trough under-corrected",
        xy=(13.0, 0.040), xytext=(5.0, 0.080),
        fontsize=10, color="#B7770D", fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#B7770D", lw=1.5),
        bbox=dict(boxstyle="round,pad=0.4", fc="#FEF5E0", ec="#B7770D", alpha=0.95),
    )

    ax1.set_xlim(0, 24)
    ax1.set_ylim(0.02, 0.285)
    ax1.set_xticks(range(0, 25, 2))
    ax1.set_xticklabels([f"{int(x):02d}:00" for x in range(0, 25, 2)],
                        color="#333333", fontsize=10, rotation=25)
    ax1.set_yticks([0.04, 0.08, 0.12, 0.16, 0.20, 0.24])
    ax1.set_yticklabels([f"${v:.2f}" for v in [0.04, 0.08, 0.12, 0.16, 0.20, 0.24]],
                        color="#333333", fontsize=10)
    ax1.set_ylabel("Price  ($/kWh)", color="#444444", fontsize=11)
    ax1.set_title(f"QLD Duck Curve — Actual vs AEMO PD7DAY Forecast (stylised)",
                  color="#111111", fontsize=13, fontweight="bold", pad=8)
    ax1.legend(handles=[l1, l2, l3], facecolor=_PAN, edgecolor="#CCCCCC",
               labelcolor="#111111", fontsize=11, loc="upper left", framealpha=0.95)
    ax1.tick_params(colors="#666666", length=3)
    ax1.grid(axis="y", color=_GRD, lw=0.7, zorder=0)
    for sp in ax1.spines.values():
        sp.set_edgecolor("#CCCCCC")

    # ─── Panel B: Heatmap ─────────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, 0])
    ax2.set_facecolor(_PAN)

    a_mat = np.array([[data[(h, t)][1] for t in _TODS] for h in _HORIZONS])
    norm  = TwoSlopeNorm(vmin=0.0, vcenter=1.0, vmax=1.7)
    cmap  = plt.cm.RdYlGn

    ax2.imshow(a_mat.T, cmap=cmap, norm=norm, aspect="auto",
               extent=[-0.5, 3.5, -0.5, 3.5])

    for i, hor in enumerate(_HORIZONS):
        for j, tod in enumerate(_TODS):
            n, a, b = data[(hor, tod)]
            conf    = "●●●" if n >= 40 else ("●●○" if n >= 15 else "●○○")
            cell_rgb = cmap(norm(a))[:3]
            lum   = 0.299 * cell_rgb[0] + 0.587 * cell_rgb[1] + 0.114 * cell_rgb[2]
            tcol  = "#000000" if lum > 0.45 else "#FFFFFF"
            b_sign = "+" if b >= 0 else "\u2212"
            ax2.text(i, j, f"a={a:.2f}  b={b_sign}{abs(b):.3f}\nn={n}  {conf}",
                     ha="center", va="center", fontsize=9,
                     color=tcol, fontweight="bold" if n >= 40 else "normal",
                     linespacing=1.6)

    ax2.set_xticks(range(4))
    ax2.set_xticklabels(_HOR_LABELS, color="#111111", fontsize=11)
    ax2.set_yticks(range(4))
    ax2.set_yticklabels(_TOD_LABELS, color="#111111", fontsize=11)
    ax2.set_title(
        "Calibration: calibrated = a \u00d7 raw + b\n"
        "(a<1 = over-forecast  \u00b7  a=1 = none  \u00b7  a>1 = under-forecast)",
        color="#111111", fontsize=11, fontweight="bold", pad=8,
    )
    ax2.tick_params(colors="#666666", length=0)
    for sp in ax2.spines.values():
        sp.set_edgecolor("#CCCCCC")

    im_proxy = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    im_proxy.set_array([])
    cbar = fig.colorbar(im_proxy, ax=ax2, fraction=0.04, pad=0.02)
    cbar.ax.tick_params(colors="#444444", labelsize=9)
    cbar.ax.set_ylabel("slope  a", color="#444444", fontsize=10)
    ax2.text(0.0, -0.10, "\u25cf\u25cf\u25cf n\u226540   \u25cf\u25cf\u25cb n\u226515   \u25cf\u25cb\u25cb n<15",
             transform=ax2.transAxes, fontsize=10, color="#555555")

    # ─── Panel C: Key bias patterns bar chart ─────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 1])
    ax3.set_facecolor(_PAN)

    # Build bar data from live coefficients — all 16 fitted cells, sorted by |a-1|
    all_cells = []
    for hor in _HORIZONS:
        for tod in _TODS:
            n, a, b = data[(hor, tod)]
            if n > 0:
                hor_short = hor.replace("h", "").replace("_", "\u2013") + "h"
                tod_short = tod[:4].capitalize()
                lbl = f"{tod_short}\n{hor_short}\n(n={n})"
                all_cells.append((lbl, a, tod, n))

    # Sort by |a - 1| descending, take top 11
    all_cells.sort(key=lambda x: abs(x[1] - 1.0), reverse=True)
    bars_data = all_cells[:11]

    xpos   = range(len(bars_data))
    a_vals = [b[1] for b in bars_data]
    cols   = [_TOD_COL[b[2]] for b in bars_data]
    xlbls  = [b[0] for b in bars_data]

    ax3.bar(list(xpos), a_vals, color=cols, alpha=0.80,
            edgecolor="#333344", lw=0.8, zorder=3)
    ax3.axhline(1.0, color="#444444", lw=1.4, ls="--", alpha=0.7,
                zorder=4, label="a=1 (passthrough)")
    ax3.set_ylim(0, max(2.0, max(a_vals) + 0.3))
    ax3.set_yticks([0, 0.5, 1.0, 1.5, 2.0])
    ax3.set_yticklabels(["0", "0.5", "1.0", "1.5", "2.0"], color="#333333", fontsize=10)
    ax3.set_ylabel("OLS slope  a   (calibrated = a \u00d7 raw + b)", color="#444444", fontsize=11)
    ax3.set_xticks(list(xpos))
    ax3.set_xticklabels(xlbls, color="#333333", fontsize=9, rotation=0)
    subtitle = f"{obs_count} obs  \u00b7  {fitted_str}  \u00b7  {region}" if obs_count else f"{fitted_str}  \u00b7  {region}"
    ax3.set_title(
        f"Key Systematic Bias Patterns\n({subtitle})",
        color="#111111", fontsize=11, fontweight="bold", pad=8,
    )
    ax3.tick_params(colors="#666666", length=3)
    ax3.grid(axis="y", color=_GRD, lw=0.7, zorder=0)
    for sp in ax3.spines.values():
        sp.set_edgecolor("#CCCCCC")

    for xi, (lbl, av, tod, n) in enumerate(bars_data):
        ax3.text(xi, av + 0.05, f"{av:.2f}", ha="center", va="bottom",
                 fontsize=9, color="#111111", fontweight="bold")

    ax3.legend(facecolor=_PAN, edgecolor="#CCCCCC", labelcolor="#111111",
               fontsize=10, loc="upper right", framealpha=0.95)

    # ── Suptitle & footer ──────────────────────────────────────────────────────
    fig.suptitle(f"NEM PD7DAY \u00b7 {region} AEMO Forecast Bias Analysis",
                 color="#111111", fontsize=15, fontweight="bold", y=0.975)
    fig.text(
        0.5, 0.015,
        f"Source: AEMO PD7DAY forecasts vs TradingIS actuals  \u00b7  "
        f"{obs_count} observations  \u00b7  {fitted_str}  \u00b7  {region}  \u00b7  "
        f"github.com/purcell-lab/nem_pd7day",
        ha="center", fontsize=9.5, color="#666677",
    )

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
