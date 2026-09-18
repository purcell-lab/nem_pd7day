"""Tests for forecast_chart.py: rendering, and the measured placement of every
label the seven day forecast chart draws.

History, condensed from the files these tests came from:

* Spike callouts, issue #84. Until the camera wrote ``spike_credible`` no chart
  had ever drawn a callout box, and rendering one showed three defects: boxes
  tiered by cluster index on a two value cycle (nine callouts on a synthetic
  run produced 21 overlapping pairs); boxes offset a fixed number of points
  above the clip line while the y span is not fixed, so a p10 at the
  -$1000/MWh market floor drew the box 55 px above the axes top, over the
  title; and the label quoting the calibrated value, so a $12.00/kWh raw spike
  was annotated "$0.18/kWh". Only a True ``spike_credible`` draws a callout;
  None and absent draw nothing.

* The clip line strip, PR #90. Everything above the clip line used to be
  positioned as a fraction of CLIP_Y. Reserving headroom for the callout
  tiers raises the axis top, which squeezed those fractions toward the daily
  extreme labels sitting 9 pt above their markers: on main the clip label and
  the first day's maximum cleared each other by a tenth of a pixel, and the
  headroom reservation turned that into a measured 3.4 px overlap. The strip
  is now allocated in points (9 extremes, 17 clip label, 28/38/48 notice
  tiers, 58/76 callout tiers) and does not move with the y limits. Review
  round three found the leader lines were kept off nothing: the line from the
  first evening spike ran through the clip label and the first day's maximum.

* Daily extreme and day divider labels, issue #93. Both were drawn at a fixed
  offset with no collision management: over the right hand $/MWh tick labels
  whenever the extreme fell in the last hours of the chart (40 of 60
  synthetic charts, identically before and after the PR #90 callout work),
  and across a legend row on the maintainer's fixture (the Mon 7 Sep maximum,
  which changed row when the callout headroom moved the axis top).
  ``place_movable_labels`` measures real bounding boxes off the drawn canvas,
  tries a fan of offsets nearest first and takes the first placement inside
  the axes and clear of everything. The measurement here is deliberately
  general: ``all_text_items`` scores every visible text on the figure against
  every other, both axes' ticks, the title, every annotation and the legend
  frame, because the two narrower PR #90 sweeps were each silent about this.
  Only matplotlib's own bottom left corner, where the first x tick and the
  first y tick graze by well under a pixel, is allowed to remain.

* No em dash or en dash in anything a chart draws, issue #92. Chart PNGs are
  dashboard output. The static check parses every module that imports
  matplotlib; the dynamic check instruments ``Text.set_text`` and renders
  every chart, which catches labels assembled at runtime (the bias chart's
  horizon tick labels were one).

* Font weight: ``fontweight='semibold'`` maps to 600, which the bundled DejaVu
  Sans does not ship, so every render logged "findfont: Failed to find font
  weight semibold". 700 is what was drawn anyway; only the log was affected.

Synthetic data throughout. The sweeps show the placement holds across the
shapes that move these labels, not that any of these shapes occur in the
market. Sweep points were reduced by rendering every original point and
keeping one chart per distinct layout (placement decisions and collision
geometry), always keeping the extremes and every point that failed with the
guarded heuristic reverted; the constants below say what was dropped and why.
"""
from __future__ import annotations

import ast
import datetime
import io
import logging
import math
import os
import random
import re
import types
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.axes  # noqa: E402
import matplotlib.figure  # noqa: E402
import matplotlib.text  # noqa: E402
import pytest  # noqa: E402

from support import NEM_TZ, PKG_DIR, load  # noqa: E402

fc = load("forecast_chart")
_engine_mod = load("calibration_engine")
render_forecast_chart = fc.render_forecast_chart
SPIKE_THRESHOLD = _engine_mod.SPIKE_THRESHOLD

RUN_DT = datetime.datetime.now(NEM_TZ).replace(
    hour=4, minute=0, second=0, microsecond=0
) - timedelta(days=1)


# ── Fixture builders ──────────────────────────────────────────────────────────

def chart_entry(i, raw, calibrated=0.18, p10=None, credible="omit", first_run=False):
    """One chart data row for the ``i``th half hour after RUN_DT."""
    start = RUN_DT + timedelta(minutes=30 * (i + 1))
    entry = {
        "nemtime": (start + timedelta(minutes=30)).isoformat(),
        "time": start.isoformat(),
        "raw_value": raw,
        "calibrated": calibrated,
        "p10": calibrated * 0.9 if p10 is None else p10,
        "p50": calibrated,
        "p90": calibrated * 1.1,
        "calibrated_source": "isotonic",
        "horizon_hours": round((i + 1) * 0.5, 1),
        "forecast_run_at": RUN_DT.isoformat(),
        "spike_first_run": first_run,
    }
    if credible != "omit":
        entry["spike_credible"] = credible
    return entry


def lor2_annotation(from_h=5, to_h=8):
    """A live LOR2 grid stress notice ``from_h`` to ``to_h`` hours after RUN_DT."""
    return types.SimpleNamespace(
        is_cancelled=False, notice_type="LOR", level=2,
        period_from=RUN_DT + timedelta(hours=from_h),
        period_to=RUN_DT + timedelta(hours=to_h), notice_id="n1",
    )


def flat_chart(n, spikes, level=0.18, floor=None):
    """``n`` intervals at a flat calibrated ``level`` with credible ``spikes``
    ({index: raw}) and an optional p10 floor on the first interval."""
    return [
        chart_entry(i, spikes.get(i, 0.05), calibrated=level,
                    p10=floor if i == 0 else None,
                    credible=True if i in spikes else "omit")
        for i in range(n)
    ]


def seven_day_fixture(with_spikes=True):
    """A full seven day diurnal chart, the shape the #90 and #93 reports came on.

    336 intervals matter. The clip line label is anchored to the third interval
    and so only occupies the leftmost tenth of the chart width; the label it
    collided with is the first day's maximum, which in this shape falls in the
    first evening and lands inside that tenth. The legend sits in the upper
    right, so only a daily maximum in the last day or two can reach it, and
    only a diurnal shape puts a maximum up there in the evening. A flat
    fixture reproduces neither collision.
    """
    spikes = {}
    if with_spikes:
        for day, peak in ((0, 8.4), (1, 14.2), (3, 22.0)):
            first = 27 + 48 * day
            for k, mult in enumerate((0.45, 1.0, 0.7)):
                spikes[first + k] = peak * mult
    data = []
    for i in range(336):
        start = RUN_DT + timedelta(minutes=30 * (i + 1))
        hour = start.hour + start.minute / 60.0
        shape = (
            0.09
            + 0.06 * math.sin((hour - 9) / 24 * 2 * math.pi)
            - 0.04 * math.exp(-((hour - 12) ** 2) / 8)
        )
        raw = spikes.get(i, max(-0.02, shape))
        cal = min(raw, 0.22) if raw < SPIKE_THRESHOLD else 0.19
        data.append(chart_entry(i, raw, calibrated=cal,
                                credible=True if raw >= SPIKE_THRESHOLD else "omit"))
    return data


def diurnal_chart(peak, floor, seed, n=336):
    """The #93 measurement family: price level by p10 floor by seed."""
    rng = random.Random(seed)
    data = []
    for i in range(n):
        start = RUN_DT + timedelta(minutes=30 * (i + 1))
        hour = start.hour + start.minute / 60.0
        shape = peak * (0.55 + 0.45 * math.sin((hour - 9) / 24 * 2 * math.pi))
        cal = max(floor, shape + rng.gauss(0, peak * 0.08))
        p10 = cal - abs(peak) * 0.2 if floor == 0.0 else floor
        data.append(chart_entry(i, cal * 1.05, calibrated=round(cal, 5),
                                p10=round(p10, 5)))
    return data


def _make_interval(
    nemtime: str = "2026-05-01T08:00:00+10:00",
    raw_value: float = 0.08,
    calibrated: float = 0.075,
    p10: float = 0.06,
    p90: float = 0.09,
    calibrated_source: str = "ols",
    horizon_hours: float = 1.0,
    forecast_run_at: str | None = None,
    spike_first_run: bool = True,
) -> dict:
    d = {
        "nemtime": nemtime,
        "raw_value": raw_value,
        "calibrated": calibrated,
        "p10": p10,
        "p90": p90,
        "calibrated_source": calibrated_source,
        "horizon_hours": horizon_hours,
        "spike_first_run": spike_first_run,
    }
    if forecast_run_at is not None:
        d["forecast_run_at"] = forecast_run_at
    return d


def _make_forecast(n: int = 10, base_hour: int = 7, forecast_run_at: str | None = None) -> list[dict]:
    """``n`` hourly intervals starting from ``base_hour`` on 1 May 2026."""
    intervals = []
    for i in range(n):
        hour = (base_hour + i) % 24
        day = 1 + (base_hour + i) // 24
        intervals.append(_make_interval(
            nemtime=f"2026-05-{day:02d}T{hour:02d}:30:00+10:00",
            raw_value=0.05 + i * 0.01,
            calibrated=0.048 + i * 0.009,
            p10=0.04 + i * 0.008,
            p90=0.06 + i * 0.012,
            forecast_run_at=forecast_run_at,
        ))
    return intervals


# ── One rendered chart, measured once and shared ──────────────────────────────

_NOTICE_LABELS = {"LOR1", "LOR2", "LOR3", "MSL1", "MSL2", "MSL3"}
# all_text_items names each kind with the index of the axes it was found on, so
# "ytick1" is a tick label on the twinx $/MWh axis. A pair made only of tick
# labels is out of scope: the chart code does not choose where ticks go.
_TICK_KINDS = ("xtick", "ytick")
# Day divider labels are the only text drawn as a weekday and day of month.
_DIVIDER_TEXT = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} \w{3}$")


def _is_tick(kind: str) -> bool:
    return kind.startswith(_TICK_KINDS)


def strip_family(text):
    """Name the label family, or None for text that does not share the strip.

    Everything above the clip line competes for the same thin band: the clip
    line label, the per day extreme labels, the grid stress notice labels and
    the spike callout boxes. Axis tick labels and the day divider labels are
    not in this strip; the whole figure is measured against itself by
    ``Chart.hits`` instead.
    """
    if text.startswith("raw $"):
        return "callout"
    if text.startswith("clip:"):
        return "clip"
    if text in _NOTICE_LABELS:
        return "notice"
    if re.fullmatch(r"\$\d+\.\d{3}", text):
        return "extreme"
    return None


class Chart:
    """Render one chart and measure everything the tests below ask about.

    ``render_forecast_chart`` returns PNG bytes and keeps no reference to its
    figure, so the figure is recovered by watching which axes it draws text
    on; the placement report is captured by wrapping ``place_movable_labels``
    for the duration of the render. Every measurement is taken from what
    matplotlib painted, so a test here fails if the placement code is wrong
    and also if it is right but does not survive the final draw.

    Measured with the figure's own renderer after the final draw. The
    renderer saves at dpi 110 with ``bbox_inches='tight'``, which lays the
    artists out for a different canvas than ``get_window_extent`` reports in,
    so unless ``full_png`` is set the save is redirected to the figure's own
    dpi with no tight bbox: the file is not what these tests measure, the
    save is the last statement of the renderer after every placement decision
    has been made, and the extents, decisions and collision pairs were checked
    identical to a redraw at the original settings on 61 charts across all the
    sweep shapes below. With ``full_png`` the real save runs and the figure is
    redrawn before measuring.

    Attributes: ``png``; ``report`` (label, mode) from ``place_movable_labels``;
    ``items`` from ``fc.all_text_items``; ``hits`` every pair of text things
    overlapping by more than half a pixel, as (kind_a, text_a, kind_b, text_b,
    ox, oy); ``axes_box``; ``callouts`` (label, painted box) for every callout
    still on the axes, since a removed artist still answers
    ``get_window_extent`` with wherever it last was; ``leaders`` (label,
    vertices) for every visible leader line; ``strip`` (family, label, box);
    ``texts`` (label, box) for every other visible text including legend
    entries, for the leader line check.
    """

    def __init__(self, data, annotations=None, *, full_png=False):
        artists: list = []
        self.report: list = []
        real_place = fc.place_movable_labels
        real_ann = matplotlib.axes.Axes.annotate
        real_txt = matplotlib.axes.Axes.text
        real_savefig = matplotlib.figure.Figure.savefig

        def spy_place(fig, ax, movable, other_axes=()):
            out = real_place(fig, ax, movable, other_axes=other_axes)
            self.report.extend(out)
            return out

        def spy_ann(ax, *args, **kwargs):
            artist = real_ann(ax, *args, **kwargs)
            artists.append(artist)
            return artist

        def spy_txt(ax, *args, **kwargs):
            artist = real_txt(ax, *args, **kwargs)
            artists.append(artist)
            return artist

        def cheap_savefig(fig, fname, **kwargs):
            kwargs = dict(kwargs, dpi=fig.dpi)
            kwargs.pop("bbox_inches", None)
            return real_savefig(fig, fname, **kwargs)

        fc.place_movable_labels = spy_place
        matplotlib.axes.Axes.annotate = spy_ann
        matplotlib.axes.Axes.text = spy_txt
        if not full_png:
            matplotlib.figure.Figure.savefig = cheap_savefig
        try:
            self.png = render_forecast_chart(data, "QLD1", annotations=annotations)
        finally:
            fc.place_movable_labels = real_place
            matplotlib.axes.Axes.annotate = real_ann
            matplotlib.axes.Axes.text = real_txt
            matplotlib.figure.Figure.savefig = real_savefig

        assert self.png, "render returned no bytes"
        live = [a for a in artists if a.figure is not None]
        assert live, "no text was drawn, so there is nothing to measure"
        fig = live[0].figure
        if full_png:
            fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        ax = fig.axes[0]

        self.items = fc.all_text_items(fig, renderer)
        self.hits = []
        for i in range(len(self.items)):
            for j in range(i + 1, len(self.items)):
                ki, ti, a = self.items[i]
                kj, tj, b = self.items[j]
                ox = min(a[2], b[2]) - max(a[0], b[0])
                oy = min(a[3], b[3]) - max(a[1], b[1])
                if ox > 0.5 and oy > 0.5:
                    self.hits.append((ki, ti, kj, tj, round(ox, 2), round(oy, 2)))
        self.axes_box = ax.get_window_extent(renderer)

        callouts = [a for a in artists
                    if str(a.get_text()).startswith("raw $") and a.axes is not None]
        self.callouts = [(a.get_text(), a.get_bbox_patch().get_window_extent(renderer))
                         for a in callouts]
        # A FancyArrowPatch reports its path in display coordinates with an
        # identity transform, so its vertices are the polyline on the image.
        self.leaders = []
        for a in callouts:
            arrow = getattr(a, "arrow_patch", None)
            if arrow is not None and arrow.get_visible():
                verts = [(float(x), float(y)) for x, y in arrow.get_path().vertices]
                self.leaders.append((a.get_text(), verts))

        # Annotation.get_window_extent covers the leader arrow as well as the
        # text, so a label with a bbox patch is measured from the patch and one
        # without as plain text.
        self.strip = []
        for a in artists:
            if a.axes is None:
                continue
            family = strip_family(str(a.get_text()))
            if family is not None:
                self.strip.append((family, str(a.get_text()), _painted_box(a, renderer)))

        skip = {id(a) for a in callouts}
        self.texts = []
        for axes in fig.axes:
            candidates = list(axes.texts) + [axes.title]
            candidates += list(axes.get_xticklabels()) + list(axes.get_yticklabels())
            legend = axes.get_legend()
            if legend is not None:
                candidates += list(legend.get_texts())
            for art in candidates:
                if id(art) in skip or not art.get_visible():
                    continue
                label = str(art.get_text())
                if not label.strip():
                    continue
                box = _painted_box(art, renderer)
                if box.width > 0 and box.height > 0:
                    self.texts.append((label, box))


def _painted_box(art, renderer):
    patch = art.get_bbox_patch() if hasattr(art, "get_bbox_patch") else None
    if patch is not None:
        return patch.get_window_extent(renderer)
    return matplotlib.text.Text.get_window_extent(art, renderer)


_CHARTS: dict = {}


def chart(key, build, notice=None, **kw) -> Chart:
    """The measured chart for ``key``, rendered on first use and then shared.

    ``build`` is a zero argument callable returning the chart data and
    ``notice`` an optional (from_h, to_h) for a LOR2 annotation; both are
    deterministic functions of the key. Sharing is what lets several tests
    assert different things about one fixture at the price of one render.
    """
    full = (key, notice)
    if full not in _CHARTS:
        annotations = [lor2_annotation(*notice)] if notice else None
        _CHARTS[full] = Chart(build(), annotations, **kw)
    return _CHARTS[full]


def measured_diurnal(peak, floor, seed, n=336) -> Chart:
    return chart(("diurnal", peak, floor, seed, n),
                 lambda: diurnal_chart(peak, floor, seed, n=n))


def measured_seven_day(with_spikes=True, notice=None) -> Chart:
    return chart(("seven_day", with_spikes),
                 lambda: seven_day_fixture(with_spikes), notice=notice)


# ── Geometry helpers ──────────────────────────────────────────────────────────

def overlapping_pairs(boxes):
    """Indices of callout boxes that overlap each other at all."""
    out = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i][1], boxes[j][1]
            if a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1:
                out.append((i, j))
    return out


def outside(boxes, axes_box):
    """Indices of callout boxes not wholly inside the axes."""
    return [
        i for i, (_label, b) in enumerate(boxes)
        if b.y1 > axes_box.y1 or b.y0 < axes_box.y0
        or b.x0 < axes_box.x0 or b.x1 > axes_box.x1
    ]


def strip_collisions(items):
    """Pairs of strip labels whose boxes overlap by more than half a pixel."""
    bad = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (f1, t1, a), (f2, t2, b) = items[i], items[j]
            ox = min(a.x1, b.x1) - max(a.x0, b.x0)
            oy = min(a.y1, b.y1) - max(a.y0, b.y0)
            if ox > 0.5 and oy > 0.5:
                bad.append((f"{f1}:{t1}", f"{f2}:{t2}", round(ox, 1), round(oy, 1)))
    return bad


def _segment_hits_box(p0, p1, box, margin=0.5):
    """Liang Barsky, written out here rather than imported from the renderer.

    A test that calls the code under test to decide whether the code under test
    is right proves nothing, so the clipping is independent.
    """
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    lo, hi = 0.0, 1.0
    edges = (
        (-dx, x0 - (box.x0 + margin)),
        (dx, (box.x1 - margin) - x0),
        (-dy, y0 - (box.y0 + margin)),
        (dy, (box.y1 - margin) - y0),
    )
    for p, q in edges:
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            lo = max(lo, t)
        else:
            hi = min(hi, t)
        if lo > hi:
            return False
    return True


def leader_text_collisions(leaders, texts):
    bad = []
    for label, verts in leaders:
        for a, b in zip(verts, verts[1:]):
            for other, box in texts:
                if _segment_hits_box(a, b, box):
                    bad.append((label, other))
    return sorted(set(bad))


def box_text_collisions(boxes, texts):
    bad = []
    for label, box in boxes:
        for other, ob in texts:
            ox = min(box.x1, ob.x1) - max(box.x0, ob.x0)
            oy = min(box.y1, ob.y1) - max(box.y0, ob.y0)
            if ox > 0.5 and oy > 0.5:
                bad.append((label, other))
    return sorted(set(bad))


def managed_label_collisions(hits):
    """Drop the pairs the chart code does not control, keep everything else.

    Only tick against tick is dropped. Anything involving a label this module
    places, the title, an axis label or the legend is kept.
    """
    return [h for h in hits if not (_is_tick(h[0]) and _is_tick(h[2]))]


# ── Basic rendering ───────────────────────────────────────────────────────────

def _passthrough_beyond_48h():
    """Normal forecast plus a passthrough_high interval at a 72 h horizon,
    which the horizon gate must not turn into a callout."""
    run_at = datetime.datetime(2026, 5, 15, 7, 30, tzinfo=NEM_TZ).isoformat()
    data = _make_forecast(10, forecast_run_at=run_at)
    data.append(_make_interval(
        nemtime="2026-05-18T07:30:00+10:00", raw_value=8.99, calibrated=8.99,
        p10=7.50, p90=10.00, calibrated_source="passthrough_high",
        horizon_hours=72.0, forecast_run_at=run_at, spike_first_run=False,
    ))
    return data


def _seven_days_with_zones():
    """336 intervals with forecast_run_at, so all three confidence zones draw."""
    run_at = datetime.datetime(2026, 5, 15, 7, 30, tzinfo=NEM_TZ)
    return [
        _make_interval(
            nemtime=(run_at + timedelta(minutes=30 * i)).isoformat(),
            raw_value=0.08 + (i % 20) * 0.003, calibrated=0.07 + (i % 20) * 0.002,
            p10=0.05, p90=0.12, horizon_hours=i * 0.5,
            forecast_run_at=run_at.isoformat(),
        )
        for i in range(336)
    ]


@pytest.mark.parametrize(
    "region, build",
    [
        ("QLD1", lambda: _make_forecast(10)),
        ("NSW1", lambda: [_make_interval()]),
        ("VIC1", lambda: [
            _make_interval(nemtime=f"2026-05-01T{h:02d}:30:00+10:00",
                           raw_value=0.0, calibrated=0.0, p10=0.0, p90=0.0)
            for h in range(7, 17)
        ]),
        ("TAS1", lambda: [
            _make_interval(nemtime=f"2026-05-01T{h:02d}:30:00+10:00",
                           raw_value=5.0, calibrated=5.0, p10=4.0, p90=6.0,
                           calibrated_source="passthrough_high")
            for h in range(16, 21)
        ]),
        ("QLD1", lambda: [
            {"nemtime": f"2026-05-01T{h:02d}:30:00+10:00", "raw_value": 0.05 + h * 0.005}
            for h in range(7, 17)
        ]),
        ("QLD1", lambda: [
            _make_interval(nemtime=f"2026-05-01T{h:02d}:30:00+10:00",
                           raw_value=0.02, calibrated=0.015, p10=-0.01, p90=0.04)
            for h in range(7, 17)
        ]),
        ("QLD1", lambda: [{"nemtime": "not-a-date", "raw_value": 0.1}, _make_interval()]),
        ("QLD1", _passthrough_beyond_48h),
        ("QLD1", _seven_days_with_zones),
    ],
    ids=[
        "ten_intervals", "single_interval", "all_zero", "all_passthrough_high",
        "no_calibration_fields", "negative_p10", "invalid_nemtime_skipped",
        "passthrough_high_beyond_48h", "seven_days_with_confidence_zones",
    ],
)
def test_render_returns_png_bytes(region, build):
    """Every shape renders to a PNG without raising.

    All zero values, all passthrough_high intervals, rows with no calibration
    fields (which fall back to raw_value), a negative p10 (y_min below zero),
    an unparseable nemtime (skipped, not fatal) and the full zoned chart.
    """
    result = render_forecast_chart(build(), region)
    assert isinstance(result, bytes)
    assert result[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(result) > 1000


def test_render_empty_list_returns_empty():
    assert render_forecast_chart([], "QLD1") == b""


def test_placeholder_png_is_valid_png():
    data = fc._placeholder_png()
    assert isinstance(data, bytes)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_forecast_chart_no_matplotlib():
    """render_forecast_chart falls back to a placeholder PNG without matplotlib."""
    from unittest.mock import patch

    _real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _mock_import(name, *args, **kwargs):
        if name == "matplotlib" or name.startswith("matplotlib."):
            raise ImportError(f"No module named '{name}'")
        return _real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_mock_import):
        result = render_forecast_chart([], "QLD1")
    assert isinstance(result, bytes)
    assert result[:4] == b"\x89PNG"


# ── Callout eligibility: horizon gate and persistence ─────────────────────────

@pytest.mark.parametrize(
    "raw, horizon, first_run, eligible, style",
    [
        (20.30, 72.0, False, False, ""),      # beyond 48 h, whatever the value
        (20.30, 168.0, False, False, ""),
        (20.30, 48.0, False, False, ""),      # exactly 48 h is suppressed
        (1.50, 10.0, False, True, "confirmed"),   # within 24 h: $1.50 qualifies
        (1.49, 10.0, False, False, ""),
        (3.00, 36.0, False, True, "confirmed"),   # 24 to 48 h: $3.00 qualifies
        (2.99, 36.0, False, False, ""),
        (5.0, 10.0, True, True, "candidate"),     # first run: candidate, not confirmed
    ],
)
def test_spike_callout_eligibility(raw, horizon, first_run, eligible, style):
    assert fc._is_spike_callout_eligible(raw, horizon_hours=horizon,
                                         spike_first_run=first_run) == (eligible, style)


# ── Callout drawing, issue #84 ────────────────────────────────────────────────

def _single_spike(floor=None):
    data = [chart_entry(i, 0.05) for i in range(96)]
    if floor is not None:
        data[5] = chart_entry(5, 0.05, p10=floor)
    data[20] = chart_entry(20, 12.0, credible=True)
    return data


def test_callout_reports_the_raw_spike_not_the_calibrated_value():
    """A spike callout that quotes the calibrated value says nothing.

    The label was the cluster's maximum calibrated value, so a $12.00/kWh raw
    forecast was annotated with a number near the clip line that the
    calibrated line already draws. The box also stays inside the axes: this
    chart's y_min is the -0.04 default, the shallow end of the p10 floor sweep
    below.
    """
    c = chart(("single_spike", None), _single_spike)
    assert len(c.callouts) == 1, f"expected one callout, got {len(c.callouts)}"
    assert "12.00" in c.callouts[0][0], f"callout label was {c.callouts[0][0]!r}"
    assert outside(c.callouts, c.axes_box) == []


@pytest.mark.parametrize(
    "credible, first_run",
    [(None, False), (False, False), ("omit", False), (True, True)],
    ids=["none", "false", "absent", "first_run"],
)
def test_only_a_confirmed_true_spike_draws_a_callout_box(credible, first_run):
    """None, False and absent all draw nothing: only True is a confirmed spike.

    A True spike seen for the first time draws a grey candidate marker and no
    box either, so the first chart rendered after #84 landed carried triangles
    and no boxes at all.
    """
    data = [chart_entry(i, 0.05) for i in range(96)]
    data[20] = chart_entry(20, 12.0, credible=credible, first_run=first_run)
    c = Chart(data)
    assert c.callouts == [], f"spike_credible={credible!r} drew {len(c.callouts)} callouts"


@pytest.mark.parametrize("floor", [-1.00, -3.00])
def test_callout_stays_inside_the_axes_when_p10_reaches_the_market_floor(floor):
    """The boxes are offset in points and the y span is not fixed.

    A p10 at the -$1000/MWh floor stretches the axis until the fixed offset
    lands outside it. On main the box is drawn 55 px above the axes top, where
    it is painted over the chart title. The four original floors -0.04, -0.30,
    -1.00 and -3.00 all placed the box identically, 61 px below the axes top;
    -0.04 is the single spike chart above and -0.30 is dropped.
    """
    c = chart(("single_spike", floor), lambda: _single_spike(floor))
    assert len(c.callouts) == 1
    assert outside(c.callouts, c.axes_box) == [], (
        f"p10 floor {floor} put the callout outside the axes: "
        f"box={c.callouts[0][1]} axes={c.axes_box}"
    )


def test_callout_does_not_land_on_a_grid_stress_label():
    """LOR and MSL labels sit in the same strip of chart above the clip line.

    The notice labels live in a band of their own below the callout tiers, so
    the two cannot collide vertically at all. The horizontal seeding of the
    tier search is still there as a second line of defence.
    """
    c = chart(("single_spike", None), _single_spike, notice=(9.5, 11.5))
    assert len(c.callouts) == 1
    assert outside(c.callouts, c.axes_box) == []
    assert any(f == "notice" for f, _t, _b in c.strip), (
        "the LOR2 label was not drawn, so this test proves nothing"
    )
    assert any(f == "callout" for f, _t, _b in c.strip), "no callout drawn"
    assert strip_collisions(c.strip) == [], strip_collisions(c.strip)


def test_dense_spikes_do_not_bury_the_chart_in_labels():
    """Every credible spike gets a triangle, but the labels are capped.

    A run where most intervals inside the callout window are spikes is not
    something a reader can be shown 90 boxes for. Clustering and the tier
    search between them keep the label count small; the triangles carry the
    per interval detail.
    """
    data = [chart_entry(i, 12.0, credible=True) for i in range(96)]
    data += [chart_entry(i, 0.05) for i in range(96, 336)]
    c = Chart(data)
    assert len(c.callouts) <= len(fc._CALLOUT_Y_OFFSETS_PT), (
        f"96 consecutive credible spikes drew {len(c.callouts)} callout boxes"
    )
    assert overlapping_pairs(c.callouts) == []
    assert outside(c.callouts, c.axes_box) == []


def test_render_still_returns_a_png_with_callouts_present():
    """The path had never executed with a non-empty set, so prove it does not raise."""
    data = [chart_entry(i, 0.05) for i in range(336)]
    for i in (10, 11, 40, 41, 80):
        data[i] = chart_entry(i, 12.0, credible=True)
    c = Chart(data, full_png=True)
    assert c.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert c.callouts, "no callout drawn, so the render was not exercising the new path"


# The callout layout sweep walks spike density, the depth of the p10 floor,
# the level the calibrated line sits at and the number of intervals, because
# the offending geometry was a product of all four and none of them alone.
# The 36 point grid is generated in its original order so the shared random
# stream gives each point the data it always had; the points not listed here
# either drew no callout at all (eight points, which assert nothing about
# layout) or placed their callouts identically to a listed neighbour.
_CALLOUT_SWEEP_GRID = ((0.02, 0.20, 1.0), (0.02, -1.00), (0.02, 1.00), (1, 96, 336))
_CALLOUT_SWEEP_KEPT = (
    # (spike rate, p10 floor, calibrated level, intervals)
    (0.02, 0.02, 1.00, 96),
    (0.02, 0.02, 1.00, 336),
    (0.02, -1.00, 0.02, 96),
    (0.02, -1.00, 0.02, 336),
    (0.02, -1.00, 1.00, 336),   # direct label: nowhere for a leader line
    (0.20, 0.02, 0.02, 96),
    (0.20, 0.02, 1.00, 96),
    (0.20, 0.02, 1.00, 336),
    (0.20, -1.00, 0.02, 96),
    (0.20, -1.00, 0.02, 336),
    (0.20, -1.00, 1.00, 96),    # one callout dropped, four with leader lines
    (1.00, 0.02, 0.02, 336),
    (1.00, 0.02, 1.00, 336),
    (1.00, -1.00, 0.02, 1),     # a single interval, all callout
)


def _callout_sweep_data():
    rng = random.Random(19)
    out = {}
    for rate in _CALLOUT_SWEEP_GRID[0]:
        for floor in _CALLOUT_SWEEP_GRID[1]:
            for level in _CALLOUT_SWEEP_GRID[2]:
                for n in _CALLOUT_SWEEP_GRID[3]:
                    data = []
                    for i in range(n):
                        raw = (
                            rng.uniform(3.0, 25.0)
                            if rng.random() < rate
                            else rng.uniform(0.01, max(level, 0.02))
                        )
                        data.append(chart_entry(
                            i, raw, calibrated=level,
                            p10=floor if i == 0 else None,
                            credible=True if raw >= SPIKE_THRESHOLD else "omit",
                        ))
                    out[(rate, floor, level, n)] = data
    return out


def test_callout_layout_sweep():
    """Invariant sweep: no callout ever overlaps another or leaves the axes."""
    grid = _callout_sweep_data()
    drawn = 0
    for point in _CALLOUT_SWEEP_KEPT:
        rate, floor, level, n = point
        c = chart(("callout_sweep",) + point, lambda: grid[point])
        drawn += len(c.callouts)
        assert overlapping_pairs(c.callouts) == [], (
            f"rate={rate} floor={floor} level={level} n={n}: "
            f"{overlapping_pairs(c.callouts)}"
        )
        assert outside(c.callouts, c.axes_box) == [], (
            f"rate={rate} floor={floor} level={level} n={n}: callout outside the axes"
        )
    assert drawn > 20, f"sweep drew only {drawn} callouts, so it proves little"


# ── The clip line strip, PR #90 ───────────────────────────────────────────────

def test_the_clip_label_does_not_collide_with_a_clipped_daily_maximum():
    """The regression the headroom reservation introduced, pinned.

    On main the clip label and the first day's maximum clear each other by a
    tenth of a pixel, which is luck rather than design, and the headroom
    reservation turned that into a measured overlap of 3.4 px. The strip is
    now allocated in points and does not move with the y limits.
    """
    c = measured_seven_day(notice=(13, 16))
    families = {f for f, _t, _b in c.strip}
    for needed in ("clip", "extreme", "notice", "callout"):
        assert needed in families, f"no {needed} label drawn, so this test proves nothing"
    assert strip_collisions(c.strip) == [], strip_collisions(c.strip)


# The y limits are what move these labels, so the strip sweep varies exactly
# the things that change the axis span: the price level, the depth of the p10
# floor and whether a callout is present to trigger the headroom reservation.
# The 48 point grid is generated in its original order for the same reason as
# the callout sweep. Floor -0.04 is the chart's default y_min, so every
# floor -0.04 point is the same chart as its floor 0.02 neighbour; the other
# points not listed placed every label and callout identically to a listed
# neighbour. Every floor -3.00 point that collides (notice label on clip
# label) when the strip is positioned as a fraction of CLIP_Y again is kept.
_STRIP_SWEEP_GRID = ((0.02, 0.18, 0.30, 1.00), (0.02, -0.04, -1.00, -3.00),
                     ((), (10, 11), (10, 11, 40, 41, 70)))
_NO_SPIKES, _TWO_SPIKES, _FIVE_SPIKES = _STRIP_SWEEP_GRID[2]
_STRIP_SWEEP_KEPT = (
    # (calibrated level, p10 floor, credible spike intervals)
    (0.02, 0.02, _NO_SPIKES), (0.02, 0.02, _TWO_SPIKES), (0.02, 0.02, _FIVE_SPIKES),
    (0.02, -1.00, _NO_SPIKES), (0.02, -1.00, _TWO_SPIKES), (0.02, -1.00, _FIVE_SPIKES),
    (0.02, -3.00, _NO_SPIKES), (0.02, -3.00, _TWO_SPIKES), (0.02, -3.00, _FIVE_SPIKES),
    (0.18, 0.02, _NO_SPIKES), (0.18, 0.02, _TWO_SPIKES), (0.18, 0.02, _FIVE_SPIKES),
    (0.18, -1.00, _TWO_SPIKES), (0.18, -1.00, _FIVE_SPIKES),
    (0.18, -3.00, _NO_SPIKES), (0.18, -3.00, _TWO_SPIKES), (0.18, -3.00, _FIVE_SPIKES),
    (0.30, 0.02, _NO_SPIKES),
    (0.30, -1.00, _TWO_SPIKES), (0.30, -1.00, _FIVE_SPIKES),
    (0.30, -3.00, _TWO_SPIKES), (0.30, -3.00, _FIVE_SPIKES),
    (1.00, 0.02, _NO_SPIKES), (1.00, 0.02, _TWO_SPIKES), (1.00, 0.02, _FIVE_SPIKES),
    (1.00, -1.00, _NO_SPIKES), (1.00, -1.00, _TWO_SPIKES), (1.00, -1.00, _FIVE_SPIKES),
    (1.00, -3.00, _NO_SPIKES),
)


def _strip_sweep_data():
    rng = random.Random(29)
    out = {}
    for level in _STRIP_SWEEP_GRID[0]:
        for floor in _STRIP_SWEEP_GRID[1]:
            for spikes in _STRIP_SWEEP_GRID[2]:
                data = []
                for i in range(96):
                    raw = rng.uniform(0.01, max(level, 0.02))
                    data.append(chart_entry(i, raw, calibrated=level,
                                            p10=floor if i == 0 else None))
                for i in spikes:
                    data[i] = chart_entry(i, 4.0 + i, calibrated=level,
                                          p10=floor if i == 0 else None,
                                          credible=True)
                out[(level, floor, spikes)] = data
    return out


def test_no_label_in_the_clip_line_strip_collides_across_a_y_limit_sweep():
    grid = _strip_sweep_data()
    checked = with_callouts = 0
    for point in _STRIP_SWEEP_KEPT:
        level, floor, spikes = point
        c = chart(("strip_sweep",) + point, lambda: grid[point], notice=(5, 8))
        if any(f == "callout" for f, _t, _b in c.strip):
            with_callouts += 1
        checked += 1
        assert strip_collisions(c.strip) == [], (
            f"level={level} floor={floor} spikes={spikes}: {strip_collisions(c.strip)}"
        )
    assert checked == len(_STRIP_SWEEP_KEPT) == 29, f"only {checked} charts swept"
    assert with_callouts >= 20, (
        f"only {with_callouts} charts drew a callout, so the headroom "
        "reservation was barely exercised"
    )


# ── Leader lines, PR #90 review round three ───────────────────────────────────

def test_a_leader_line_never_crosses_another_label():
    """The defect a reviewer found on the rendered image, pinned.

    The boxes were being kept off other labels but the lines joining them to
    their markers were not, and on this fixture the line from the first
    evening spike ran straight through the clip line label and through the
    first day's maximum. Both callouts now route around them.
    """
    c = measured_seven_day()
    assert c.leaders, "the fixture drew no leader lines, so this proves nothing"
    hits = leader_text_collisions(c.leaders, c.texts)
    assert hits == [], f"leader lines cross text: {hits}"
    assert box_text_collisions(c.callouts, c.texts) == []


# The shapes that broke the leader routing before. Each was originally
# rendered with and without a grid stress notice; the notice changed no
# placement decision on any of the eight, so only "clustered", whose spikes
# sit nearest the notice band, keeps its notice variant.
_AWKWARD_SHAPES = {
    "adjacent": lambda: flat_chart(96, {20: 9.0, 21: 12.0}),
    "clustered": lambda: flat_chart(96, {20: 9.0, 21: 12.0, 22: 7.0, 23: 15.0}),
    "left_edge": lambda: flat_chart(96, {0: 11.0, 1: 8.0}),
    "right_edge": lambda: flat_chart(96, {94: 11.0, 95: 8.0}),
    "both_edges": lambda: flat_chart(96, {0: 11.0, 95: 8.0}),
    "one_interval": lambda: flat_chart(1, {0: 11.0}),
    "deep_p10": lambda: flat_chart(96, {20: 9.0, 60: 14.0}, floor=-3.00),
}


def test_leader_lines_clear_every_label_across_the_awkward_shapes():
    cases = [(name, None) for name in _AWKWARD_SHAPES]
    cases += [("clustered", (5, 8)), ("seven_day", None)]
    leader_count = 0
    for name, notice in cases:
        if name == "seven_day":
            c = measured_seven_day(notice=notice)
        else:
            c = chart(("awkward", name), _AWKWARD_SHAPES[name], notice=notice)
        leader_count += len(c.leaders)
        hits = leader_text_collisions(c.leaders, c.texts)
        assert hits == [], f"{name} notices={notice is not None}: {hits}"
        over = box_text_collisions(c.callouts, c.texts)
        assert over == [], f"{name} notices={notice is not None}: {over}"
    # Eight leader lines are drawn across these nine charts (adjacent 1,
    # clustered 1 + 1, one_interval 1, deep_p10 2, seven_day 2). Fewer means
    # the routing has started degrading to direct labels: with one sideways
    # offset and no anchor lift it draws five.
    assert leader_count >= 7, (
        f"only {leader_count} leader lines drawn across {len(cases)} charts, so "
        "the sweep barely exercises the routing"
    )


# ── Daily extreme and day divider label placement, issue #93 ──────────────────

def test_text_collision_pairs_reports_overlapping_text_only():
    """The module's own collision helper agrees with the geometry: two texts
    painted on top of each other are one pair, a distant third is none."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = matplotlib.figure.Figure(figsize=(4, 3), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.text(0.5, 0.5, "alpha", transform=ax.transAxes)
    ax.text(0.5, 0.5, "alpha", transform=ax.transAxes)
    ax.text(0.05, 0.05, "far", transform=ax.transAxes)
    hits = fc.text_collision_pairs(fig)
    assert len(hits) == 1, hits
    ki, ti, kj, tj, ox, oy = hits[0]
    assert (ki, ti, kj, tj) == ("label0", "alpha", "label0", "alpha")
    assert ox > 0.5 and oy > 0.5


def test_the_measurement_itself_sees_the_legend_and_both_axes():
    """Guard the sweep before trusting it, since scope is how this defect hid.

    If ``all_text_items`` quietly stopped reporting the legend frame or the
    right hand axis, every assertion below would pass while saying nothing.
    """
    c = measured_seven_day()
    kinds = {k for k, _t, _r in c.items}
    for required in ("legend0", "title0", "xtick0", "ytick0", "ytick1"):
        assert required in kinds, f"{required} missing from {sorted(kinds)}"
    assert len(c.items) > 40, f"only {len(c.items)} text items found"


@pytest.mark.parametrize("with_spikes", [True, False], ids=["spikes", "no_spikes"])
@pytest.mark.parametrize("notice", [None, (5, 8)], ids=["no_notice", "lor2"])
def test_no_managed_label_collides_on_the_maintainer_style_fixture(with_spikes, notice):
    """The maintainer's reported case, pinned, and the whole figure around it.

    On main the Mon 7 Sep maximum overlaps the legend frame by about 39 x 10
    px, landing across a legend row, with the callouts present and with them
    absent. Nothing that overlaps the legend is acceptable, and beyond the
    legend only tick against tick may remain anywhere on the figure.
    """
    c = measured_seven_day(with_spikes, notice=notice)
    on_legend = [h for h in c.hits
                 if h[0].startswith("legend") or h[2].startswith("legend")]
    assert on_legend == [], on_legend
    assert managed_label_collisions(c.hits) == [], managed_label_collisions(c.hits)


# The price and floor sweep. The price level sets where the daily extremes sit
# relative to the legend and the clip line, the p10 floor sets the axis span
# (which moved the legend collision from one row to another) and the seed
# moves which interval of each day is the extreme, which decides whether the
# last day's label reaches the $/MWh tick gutter. Of the original 3 x 4 x 5
# grid, floor -0.04 is the chart's default y_min and so the same chart as
# floor 0.0 at peaks 0.05 and 0.2; and within a (peak, floor) cell the seeds
# listed are the ones whose placement decisions differ from each other.
_PRICE_FLOOR_SWEEP = (
    # (peak, floor, seeds)
    (0.05, 0.0, (1, 2, 3)),
    (0.05, -1.0, (1, 2, 3, 4)),
    (0.05, -3.0, (1, 2, 3)),
    (0.2, 0.0, (1, 2, 3, 4)),
    (0.2, -1.0, (1, 3, 4)),
    (0.2, -3.0, (1, 2, 3, 4, 5)),
    (0.9, 0.0, (1, 2, 3, 5)),
    (0.9, -0.04, (1, 2, 3, 4, 5)),
    (0.9, -1.0, (1, 3, 4)),
    (0.9, -3.0, (1, 3, 4, 5)),
)


def test_no_managed_label_collides_across_the_price_and_floor_sweep():
    checked = 0
    for peak, floor, seeds in _PRICE_FLOOR_SWEEP:
        for seed in seeds:
            c = measured_diurnal(peak, floor, seed)
            bad = managed_label_collisions(c.hits)
            assert bad == [], f"peak={peak} floor={floor} seed={seed}: {bad}"
            checked += 1
    assert checked == 38, f"only {checked} charts swept"


def test_nothing_overhangs_the_right_hand_axis_tick_labels():
    """The 40 of 60 manifestation from the issue, stated as its own case.

    A daily extreme in the last hours of the chart used to be centred on its
    marker regardless of how close the marker was to the right spine, so the
    label ran out over the ``$/MWh`` numbers. Inside the axes is now a hard
    requirement of the placement, which is what removes this. Measured on the
    shallow floor charts of the sweep above, which are shared with it.
    """
    offenders = []
    checked = 0
    for peak, floor, seeds in _PRICE_FLOOR_SWEEP:
        if floor < -0.04:
            continue
        for seed in seeds:
            checked += 1
            for h in measured_diurnal(peak, floor, seed).hits:
                if any(k.startswith("ytick") for k in (h[0], h[2])) and not (
                    _is_tick(h[0]) and _is_tick(h[2])
                ):
                    offenders.append((peak, floor, seed, h))
    assert checked >= 15, f"only {checked} charts checked"
    assert offenders == [], offenders


@pytest.mark.parametrize(
    "n, floor",
    [(48, 0.0), (96, -3.0), (336, 0.0), (336, -3.0)],
)
def test_the_day_divider_label_stays_inside_the_axes(n, floor):
    """Inside the axes is a hard constraint, so state it separately.

    The divider labels sit at the bottom of the plot and used to dip into the x
    tick labels below the spine on short charts. The placement rejects any
    candidate whose box is not wholly inside the axes, so a divider label that
    cannot fit is nudged rather than allowed to leave. A 12 interval chart
    starting at 04:30 crosses no midnight and so had no divider to check; 48
    is the shortest chart with one. Both floors place the dividers identically
    at 48 and 96 intervals and differently at 336.
    """
    c = measured_diurnal(0.2, floor, 7, n=n)
    ab = c.axes_box
    checked = 0
    for _kind, text, rect in c.items:
        if not _DIVIDER_TEXT.match(text):
            continue
        checked += 1
        assert (
            rect[0] >= ab.x0 - 0.5 and rect[2] <= ab.x1 + 0.5
            and rect[1] >= ab.y0 - 0.5 and rect[3] <= ab.y1 + 0.5
        ), (f"n={n} floor={floor}: divider label {text!r} at {rect} "
            f"leaves the axes {(ab.x0, ab.y0, ab.x1, ab.y1)}")
    assert checked >= 1, "no divider label drawn, so this proves nothing"


def test_placement_reports_a_mode_for_every_label_and_degrades_honestly():
    """The report is the honesty mechanism, so pin its shape and its values.

    ``place_movable_labels`` returns (label text, mode) per label. ``clear``
    means it found a free position. ``least_overlap`` means nothing was free
    and it took the least bad position, which the module logs at debug level.
    ``dropped`` means a daily extreme was removed entirely, which is only safe
    because the marker dot stays and so nothing false is printed. On the
    fixtures below every label is placed clear, and no mode outside the known
    set ever appears.
    """
    known = {"clear", "least_overlap", "dropped", "default"}
    charts = [measured_seven_day(notice=(5, 8))]
    for peak in (0.05, 0.9):
        for floor in (0.0, -3.0):
            charts.append(measured_diurnal(peak, floor, 1))
    total = 0
    modes: dict = {}
    for c in charts:
        assert c.report, "no labels were placed, so the report proves nothing"
        for label, mode in c.report:
            assert mode in known, f"unknown placement mode {mode!r}"
            assert isinstance(label, str) and label, "report entry has no text"
            modes[mode] = modes.get(mode, 0) + 1
            total += 1
    assert total >= 40, f"only {total} labels placed across {len(charts)} charts"
    assert modes.get("clear", 0) == total, (
        f"expected every label placed clear on these fixtures, got {modes}"
    )


# ── Font weight ───────────────────────────────────────────────────────────────

def test_render_emits_no_findfont_warning():
    """Chart rendering must not ask for a font weight the bundled font lacks."""
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    # findfont results are memoised per process and the warning is logged only
    # on a cache miss, so an earlier render in the same session would otherwise
    # let a regression through here unnoticed.
    from matplotlib import font_manager

    for holder in (font_manager, getattr(font_manager, "fontManager", None)):
        for name in ("_findfont_cached", "findfont"):
            cache_clear = getattr(getattr(holder, name, None), "cache_clear", None)
            if callable(cache_clear):
                cache_clear()

    logger = logging.getLogger("matplotlib.font_manager")
    handler = _Capture()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        # 48 intervals is enough to exercise the min and max annotations, which
        # are the two call sites that requested the unsupported weight.
        result = render_forecast_chart(_make_forecast(48), "QLD1")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert result[:4] == b"\x89PNG"
    findfont = [m for m in records if "findfont" in m]
    assert not findfont, f"matplotlib could not resolve a requested font: {findfont}"


# ── Static guards over every chart module ─────────────────────────────────────

def _chart_modules() -> list[str]:
    """Every module in the package that imports matplotlib, discovered, not listed.

    Discovery rather than a fixed list is the point: a chart module added
    later is guarded without anyone remembering to extend these tests.
    """
    found = []
    for name in sorted(os.listdir(PKG_DIR)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(PKG_DIR, name), encoding="utf-8") as handle:
            if "matplotlib" in handle.read():
                found.append(name[:-3])
    return found


def _parse_module(module_name: str) -> ast.AST:
    with open(os.path.join(PKG_DIR, f"{module_name}.py"), encoding="utf-8") as handle:
        return ast.parse(handle.read())


def test_chart_modules_are_discovered():
    """The discovery must actually find the chart modules, or the guards are vacuous."""
    mods = _chart_modules()
    for expected in ("forecast_chart", "iso_chart", "bias_chart", "tod_stats"):
        assert expected in mods, f"{expected} not discovered, guard would be vacuous"


@pytest.mark.parametrize("module_name", _chart_modules())
def test_chart_modules_request_only_supported_font_weights(module_name):
    # DejaVu Sans, which matplotlib bundles and these charts use, ships regular
    # and bold only. Anything else silently falls back and logs a warning.
    supported = {"normal", "regular", "bold"}
    offenders = []
    for node in ast.walk(_parse_module(module_name)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in ("fontweight", "weight"):
                continue
            if not isinstance(keyword.value, ast.Constant):
                continue
            value = keyword.value.value
            if isinstance(value, str) and value not in supported:
                offenders.append(f"line {node.lineno}: {keyword.arg}={value!r}")
    assert not offenders, f"{module_name}.py requests unsupported font weights: {offenders}"


BAD_DASHES = {"—": "em dash", "–": "en dash"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the string constants that are docstrings, which are not drawn."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


@pytest.mark.parametrize("module_name", _chart_modules())
def test_no_dash_in_chart_string_literals(module_name):
    """No em dash or en dash in any non-docstring string literal of a chart module.

    The parser resolves escaped forms such as the six digit backslash-u
    escapes the code used, and this covers literals nobody has written a
    render fixture for yet.
    """
    tree = _parse_module(module_name)
    skip = _docstring_nodes(tree)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        for ch, label in BAD_DASHES.items():
            if ch in node.value:
                offenders.append(f"{module_name}.py line {node.lineno}: {label} in {node.value!r}")
    assert not offenders, "dash characters in chart text: " + "; ".join(offenders)


# ── No dash reaches the renderer of any chart ─────────────────────────────────

def _calibration_result():
    """A CalibrationResult with fitted cells in every horizon bucket.

    Fitted cells matter here: the bias chart bar labels are built from the
    bucket key at render time, and a bucket under MIN_OBS draws no bar, so a
    thin fixture would skip exactly the label that had to be fixed.
    """
    e = _engine_mod
    models = {}
    horizons = ["h00_06", "h06_12", "h12_24", "h24_48", "h48_96", "h96plus"]
    tods = ["shoulder", "morning_ramp", "solar", "peak"]
    for i, hor in enumerate(horizons):
        for j, tod in enumerate(tods):
            key = f"{hor}__{tod}"
            a = 0.7 + 0.1 * ((i + j) % 6)
            models[key] = e.BucketModel(
                key,
                e.LinearCoeff(a=a, b=0.01, n=60 + i * 5 + j, mae=0.01, rmse=0.012),
                e.QuantileCoeff(0.1, a=a * 0.9, b=0.008, n=60),
                e.QuantileCoeff(0.5, a=a, b=0.010, n=60),
                e.QuantileCoeff(0.9, a=a * 1.1, b=0.012, n=60),
            )
    return e.CalibrationResult(
        fitted_at="2026-05-01T18:00:00+10:00",
        total_observations=500,
        models=models,
    )


def _forecast_rows():
    run = datetime.datetime(2026, 5, 1, 4, 0, tzinfo=NEM_TZ)
    rows = []
    for i in range(200):
        start = run + timedelta(minutes=30 * (i + 1))
        h = (start - run).total_seconds() / 3600.0
        raw = 0.06 + 0.02 * ((i % 12) / 12.0)
        row = {
            "nemtime": (start + timedelta(minutes=30)).isoformat(),
            "time": start.isoformat(),
            "raw_value": raw,
            "calibrated": raw * 0.95,
            "p10": raw * 0.8, "p50": raw * 0.95, "p90": raw * 1.15,
            "calibrated_source": "isotonic+stpasa" if h < 96 else "isotonic",
            "horizon_hours": round(h, 1),
            "forecast_run_at": run.isoformat(),
            "spike_first_run": False,
        }
        if i == 30:
            row["raw_value"] = 9.0
            row["spike_credible"] = True
        rows.append(row)
    return rows, run


def _notices(run):
    out = []
    for k, (kind, level) in enumerate((("LOR", 1), ("LOR", 2), ("LOR", 3),
                                       ("MSL", 1), ("MSL", 2), ("MSL", 3))):
        out.append(types.SimpleNamespace(
            is_cancelled=False, notice_type=kind, level=level,
            period_from=run + timedelta(hours=10 + 4 * k),
            period_to=run + timedelta(hours=12 + 4 * k),
            notice_id=f"n{k}",
        ))
    return out


def _capturing_set_text(seen: list[str]):
    """A replacement for ``Text.set_text`` that records every string it is given.

    ``Text.__init__`` funnels through ``set_text``, so titles, legend labels,
    tick labels and annotations all pass through this one hook whichever axes
    API created them.
    """
    original = matplotlib.text.Text.set_text

    def recording(self, s):
        if isinstance(s, str):
            seen.append(s)
        return original(self, s)

    return original, recording


def test_no_dash_in_rendered_chart_text():
    """Nothing drawn on any of the four charts contains an em dash or en dash."""
    bias_chart, iso_chart, tod_stats = load("bias_chart"), load("iso_chart"), load("tod_stats")
    result = _calibration_result()
    obs = [
        {"interval_time": f"2026-04-{18 + day:02d}T{hour:02d}:{minute:02d}:00+10:00",
         "actual_rrp": 0.04 + hour * 0.004, "pd7day_forecast": 0.05 + hour * 0.004}
        for day in range(5) for hour in range(0, 24, 2) for minute in (0, 30)
    ]
    stats = tod_stats.compute(obs)
    rows, run = _forecast_rows()

    seen: list[str] = []
    original, recording = _capturing_set_text(seen)
    matplotlib.text.Text.set_text = recording
    try:
        for png in (
            render_forecast_chart(rows, "QLD1", annotations=_notices(run)),
            bias_chart.render_chart(result, obs_count=500, region="QLD1"),
            bias_chart.render_chart(result, obs_count=500, region="QLD1", tod_stats=stats),
            iso_chart.render_iso_chart(result, iso_history=[], obs_count=500, region="QLD1"),
            tod_stats.render_chart(stats, region="QLD1"),
        ):
            assert png[:4] == b"\x89PNG"
    finally:
        matplotlib.text.Text.set_text = original

    assert len(seen) > 50, f"only {len(seen)} strings captured, fixture is too thin"
    offenders = sorted({s for s in seen if any(ch in s for ch in BAD_DASHES)})
    assert not offenders, f"dash characters reached the renderer: {offenders}"


def test_render_capture_would_notice_a_dash():
    """The capture hook is live, proven by planting a dash through the same hook.

    Without this, a broken hook would make the render check pass silently.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    seen: list[str] = []
    original, recording = _capturing_set_text(seen)
    matplotlib.text.Text.set_text = recording
    try:
        fig = matplotlib.figure.Figure()
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        ax.set_title("planted — dash")
        fig.savefig(io.BytesIO(), format="png")
    finally:
        matplotlib.text.Text.set_text = original

    assert any("—" in s for s in seen), "capture hook missed a planted em dash"
