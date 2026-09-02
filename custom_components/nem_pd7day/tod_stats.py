"""
Time-of-day statistics computed from the observation log.

Pure computation — no HA dependencies, fully testable.

Each 30-minute slot (identified by hour + minute of the interval START time)
is summarised across all recorded actuals.  The result is a TodStats dataclass
that is attached to the coordinator after each refit and consumed by both the
camera entity and the ToD sensor.
"""
from __future__ import annotations

import io
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .calibration_engine import CalibrationResult

_LOGGER = logging.getLogger(__name__)

# Number of slots in a day (48 × 30-min)
_SLOTS = 48


@dataclass
class SlotStats:
    """Statistics for a single 30-minute time-of-day slot."""

    hour: int
    minute: int
    n: int
    mean: float
    median: float
    p10: float
    p25: float
    p75: float
    p90: float
    # Mean raw PD7DAY forecast for this slot (across all observations with a raw value)
    mean_raw: float | None = None
    # Mean calibrated forecast for this slot (None if no calibration available)
    mean_calibrated: float | None = None

    @property
    def label(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def as_dict(self) -> dict:
        d = {
            "hour": self.hour,
            "minute": self.minute,
            "label": self.label,
            "n": self.n,
            "mean_kwh": round(self.mean, 6),
            "median_kwh": round(self.median, 6),
            "p10_kwh": round(self.p10, 6),
            "p25_kwh": round(self.p25, 6),
            "p75_kwh": round(self.p75, 6),
            "p90_kwh": round(self.p90, 6),
        }
        if self.mean_raw is not None:
            d["mean_raw_kwh"] = round(self.mean_raw, 6)
        if self.mean_calibrated is not None:
            d["mean_calibrated_kwh"] = round(self.mean_calibrated, 6)
        return d


@dataclass
class TodStats:
    """Aggregated time-of-day statistics for all 48 slots."""

    slots: list[SlotStats] = field(default_factory=list)
    unique_intervals: int = 0
    date_from: str = ""
    date_to: str = ""

    def slot_for_now(self, dt: datetime) -> SlotStats | None:
        """Return the SlotStats for the 30 minute slot containing ``dt``.

        Slots exist only at minute 0 and minute 30. This previously required
        exact equality on ``dt.minute``, so any state write landing on another
        minute matched nothing and the sensor rendered ``unknown`` until the
        next boundary. A restart at an arbitrary minute blanked the sensor for
        up to 30 minutes even though every slot was populated. Flooring to the
        containing slot matches how slots are labelled elsewhere and matches
        the "statistics for the slot we are in" reading. See issue #45.
        """
        minute = 0 if dt.minute < 30 else 30
        for s in self.slots:
            if s.hour == dt.hour and s.minute == minute:
                return s
        return None

    def as_attributes(self) -> dict:
        """Return a HA-friendly attributes dict (slots as list)."""
        return {
            "unique_intervals": self.unique_intervals,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "slots": [s.as_dict() for s in self.slots],
        }


def compute(
    observations: list[dict],
    calibration_result: "CalibrationResult | None" = None,
) -> TodStats:
    """
    Compute per-slot statistics from a list of observation dicts.

    Each dict must have:
      - interval_time:   ISO-8601 string (NEM time, UTC+10)
      - actual_rrp:      float | None  ($/kWh)
      - pd7day_forecast: float | None  ($/kWh, raw AEMO forecast)
      - horizon_hours:   float | None
      - hour_of_day:     int | None

    Multiple observations for the same interval_time (from different forecast
    runs) are deduplicated — actual_rrp is identical across runs for the same
    interval; raw forecasts are averaged across runs.

    If calibration_result is provided, mean_calibrated is also computed for
    each slot using the fitted OLS model for that interval's horizon + ToD.
    """
    from .calibration_engine import _bucket_key

    # Collect actuals (deduplicated) and raw forecasts (averaged across runs)
    actuals: dict[str, float]       = {}
    raw_acc: dict[str, list[float]] = defaultdict(list)
    cal_acc: dict[str, list[float]] = defaultdict(list)

    for o in observations:
        it  = o.get("interval_time")
        rrp = o.get("actual_rrp")
        raw = o.get("pd7day_forecast")
        if it is None:
            continue
        if rrp is not None and it not in actuals:
            actuals[it] = float(rrp)
        if raw is not None:
            raw_acc[it].append(float(raw))
            if calibration_result is not None:
                h   = o.get("horizon_hours")
                hod = o.get("hour_of_day")
                if h is not None and hod is not None:
                    key = _bucket_key(float(h), int(hod))
                    bm  = calibration_result.models.get(key)
                    if bm is not None:
                        cal_val = bm.apply_all(float(raw))["calibrated"]
                        cal_acc[it].append(float(cal_val))

    if not actuals:
        return TodStats()

    mean_raw: dict[str, float] = {
        it: float(np.mean(vals)) for it, vals in raw_acc.items() if vals
    }
    mean_cal: dict[str, float] = {
        it: float(np.mean(vals)) for it, vals in cal_acc.items() if vals
    }

    # Bucket by (hour, minute)
    act_buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    raw_buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    cal_buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    dates: list[str] = []

    for iso, rrp in actuals.items():
        dt  = datetime.fromisoformat(iso)
        key = (dt.hour, dt.minute)
        act_buckets[key].append(rrp)
        dates.append(iso)
        if iso in mean_raw:
            raw_buckets[key].append(mean_raw[iso])
        if iso in mean_cal:
            cal_buckets[key].append(mean_cal[iso])

    dates_sorted = sorted(dates)
    date_from = datetime.fromisoformat(dates_sorted[0]).strftime("%d %b")
    date_to   = datetime.fromisoformat(dates_sorted[-1]).strftime("%d %b %Y")

    slots: list[SlotStats] = []
    for (h, m) in sorted(act_buckets.keys()):
        vals = np.array(act_buckets[(h, m)])
        raws = raw_buckets.get((h, m))
        cals = cal_buckets.get((h, m))
        slots.append(SlotStats(
            hour=h,
            minute=m,
            n=len(vals),
            mean=float(np.mean(vals)),
            median=float(np.median(vals)),
            p10=float(np.percentile(vals, 10)),
            p25=float(np.percentile(vals, 25)),
            p75=float(np.percentile(vals, 75)),
            p90=float(np.percentile(vals, 90)),
            mean_raw=float(np.mean(raws)) if raws else None,
            mean_calibrated=float(np.mean(cals)) if cals else None,
        ))

    return TodStats(
        slots=slots,
        unique_intervals=len(actuals),
        date_from=date_from,
        date_to=date_to,
    )


def render_chart(stats: TodStats, region: str = "QLD1") -> bytes:
    """
    Render the time-of-day price chart as PNG bytes.

    Returns an empty bytes object if matplotlib is unavailable or stats is empty.
    """
    if not stats.slots:
        return b""

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.figure as mplfig
        import matplotlib.ticker as ticker
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except ImportError:
        _LOGGER.warning("tod_stats: matplotlib not available, chart unavailable")
        return b""

    BG      = "#F8F9FA"
    PAN     = "#FFFFFF"
    C_MEAN  = "#1A6FBF"
    C_MED   = "#E05A2B"
    C_IQR   = "#A8C8E8"
    C_P1090 = "#D4E8F5"

    slots   = stats.slots
    x       = np.arange(len(slots))
    means   = [s.mean   for s in slots]
    medians = [s.median for s in slots]
    p10     = [s.p10    for s in slots]
    p25     = [s.p25    for s in slots]
    p75     = [s.p75    for s in slots]
    p90     = [s.p90    for s in slots]
    counts  = [s.n      for s in slots]

    tick_pos    = [i for i, s in enumerate(slots) if s.minute == 0]
    tick_labels = [f"{slots[i].hour:02d}:00" for i in tick_pos]

    fig = mplfig.Figure(figsize=(16, 9), facecolor=BG)
    FigureCanvasAgg(fig)
    fig.patch.set_facecolor(BG)
    ax, ax2 = fig.subplots(2, 1, gridspec_kw={"height_ratios": [4, 1]})

    # ── Main chart ────────────────────────────────────────────────────────────
    ax.set_facecolor(PAN)
    ax.fill_between(x, p10, p90, color=C_P1090, alpha=0.7, label="P10-P90")
    ax.fill_between(x, p25, p75, color=C_IQR,   alpha=0.9, label="P25-P75 (IQR)")
    ax.plot(x, means,   color=C_MEAN, lw=2.0,       label="Mean",   zorder=5)
    ax.plot(x, medians, color=C_MED,  lw=1.8, ls="--", label="Median", zorder=5)
    ax.axhline(0, color="#999999", lw=0.8, ls="--", alpha=0.6)

    ax_r = ax.secondary_yaxis(
        "right",
        functions=(lambda v: v * 1000, lambda v: v / 1000),
    )
    ax_r.set_ylabel("$/MWh", color="#444444", fontsize=11)
    ax_r.tick_params(colors="#555555", labelsize=9)

    ax.set_ylabel("$/kWh", color="#444444", fontsize=11)
    ax.tick_params(axis="y", colors="#555555", labelsize=9)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.3f"))
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_labels, color="#333333", fontsize=9)
    ax.set_xlim(-0.5, len(slots) - 0.5)
    ax.set_title(
        f"{region} Actual Price by Time of Day\n"
        f"{stats.date_from} to {stats.date_to}  ·  {stats.unique_intervals} unique intervals",
        color="#111111", fontsize=13, fontweight="bold", pad=10,
    )
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.grid(axis="y", color="#DDDDDD", lw=0.7)
    ax.grid(axis="x", color="#EEEEEE", lw=0.5)
    for sp in ax.spines.values():
        sp.set_edgecolor("#CCCCCC")

    # ── Count bar ─────────────────────────────────────────────────────────────
    ax2.set_facecolor(PAN)
    ax2.bar(x, counts, color="#AAAAAA", width=0.8)
    ax2.set_xlim(-0.5, len(slots) - 0.5)
    ax2.set_xticks(tick_pos)
    ax2.set_xticklabels(tick_labels, color="#333333", fontsize=9)
    ax2.set_ylabel("n obs", color="#555555", fontsize=9)
    ax2.tick_params(axis="y", colors="#555555", labelsize=8)
    ax2.grid(axis="y", color="#EEEEEE", lw=0.5)
    ax2.set_title("Observation count per slot", color="#555555", fontsize=9, pad=4)
    for sp in ax2.spines.values():
        sp.set_edgecolor("#CCCCCC")

    fig.tight_layout(pad=1.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=BG)
    buf.seek(0)
    return buf.read()
