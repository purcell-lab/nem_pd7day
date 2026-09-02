"""Measured placement for daily extreme and day divider labels, issue #93.

Both label families were drawn at a fixed offset from their marker with no
collision management at all: a daily extreme 9 pt above its dot, a day divider
label 0.02 date units to the right of the midnight line. Nothing looked at what
was already in those pixels, so the labels landed on other chart furniture. Two
manifestations were confirmed on the issue:

* over the right hand ``$/MWh`` tick labels, whenever the extreme falls in the
  last hours of the chart, which fired on 40 of 60 synthetic charts and did so
  identically before and after the PR #90 callout work;
* over the legend box, where on the maintainer's fixture the Mon 7 Sep daily
  maximum printed straight across a legend row. It changed which row it hit
  when the callout headroom moved the axis top, so it was not caused by the
  callouts and was not fixed by them either.

PR #90 had already built the machinery this needs for spike callouts: measure
real bounding boxes off the drawn canvas, try a fan of candidate offsets nearest
first, and take the first placement that is inside the axes and clear of
everything. ``place_movable_labels`` applies that to these two families plus the
24h confidence boundary label, reusing ``text_obstacle_rects`` rather than
adding a third narrow sweep.

The measurement here is deliberately general. Two sweeps written for PR #90
were each accurate within their own scope and each silent about this defect,
because one scored callouts against callouts and the other scored only the strip
above the clip line. ``forecast_chart.text_collision_pairs`` scores every visible
text artist on the figure against every other: both axes' tick labels and axis
labels, the title, every annotation, every free text and the legend frame. The
same function is what the production placement code measures with, so a test
passing here means the placement really did see what a reader sees.

What is allowed to remain: matplotlib's own bottom left corner, where the first
x tick label and the first y tick label graze each other by well under a
pixel. Moving axis ticks is outside this change, so the assertions below permit
tick against tick and forbid any pair that involves a label the chart places.

Synthetic data throughout. It shows the placement holds across the shapes that
move these labels, not that any of these shapes occur in the market.

Run with:  python -m pytest tests/test_chart_label_placement.py -v
or simply: python tests/test_chart_label_placement.py
"""
from __future__ import annotations

import datetime
import math
import os
import random
import re
import sys
import types
from datetime import timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.axes  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from custom_components.nem_pd7day import forecast_chart as fc  # noqa: E402

NEM_TZ = timezone(timedelta(hours=10))
RUN_DT = datetime.datetime.now(NEM_TZ).replace(
    hour=4, minute=0, second=0, microsecond=0
) - timedelta(days=1)

# all_text_items names each kind with the index of the axes it was found on, so
# "ytick1" is a tick label on the twinx $/MWh axis. A pair made only of tick
# labels is out of scope: the chart code does not choose where ticks go.
_TICK_KINDS = ("xtick", "ytick")
# Day divider labels are the only text drawn as a weekday and day of month.
_DIVIDER_TEXT = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} \w{3}$")


def _is_tick(kind: str) -> bool:
    return kind.startswith(_TICK_KINDS)


def chart_entry(i, raw, calibrated=0.18, p10=None, credible="omit", first_run=False):
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


def lor2_annotation():
    return types.SimpleNamespace(
        is_cancelled=False, notice_type="LOR", level=2,
        period_from=RUN_DT + timedelta(hours=5),
        period_to=RUN_DT + timedelta(hours=8), notice_id="n1",
    )


def maintainer_style_fixture(with_spikes=True):
    """A full seven day diurnal chart, the shape the issue was reported on.

    336 intervals matter. The legend sits in the upper right, so only a daily
    maximum in the last day or two of the chart can reach it, and only a diurnal
    shape puts a maximum up there in the evening. A flat fixture does not
    reproduce the collision.
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
        cal = min(raw, 0.22) if raw < 0.3 else 0.19
        data.append(
            chart_entry(i, raw, calibrated=cal,
                        credible=True if raw >= 0.3 else "omit")
        )
    return data


def diurnal_chart(peak, floor, seed, n=336):
    """The measurement family from the issue: price level by p10 floor by seed."""
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


def render_and_measure(data, annotations=None):
    """Render a chart and return (collision pairs, placement report).

    ``render_forecast_chart`` returns PNG bytes and keeps no reference to its
    figure, so the figure is recovered by watching which axes the renderer draws
    text on. The placement report is captured by wrapping the public
    ``place_movable_labels`` for the duration of the render.
    """
    seen_axes = []
    report = []

    real_place = fc.place_movable_labels
    real_ann = matplotlib.axes.Axes.annotate
    real_txt = matplotlib.axes.Axes.text

    def spy_place(fig, ax, movable, other_axes=()):
        out = real_place(fig, ax, movable, other_axes=other_axes)
        report.extend(out)
        return out

    def spy_ann(self, *args, **kwargs):
        artist = real_ann(self, *args, **kwargs)
        seen_axes.append(self)
        return artist

    def spy_txt(self, *args, **kwargs):
        artist = real_txt(self, *args, **kwargs)
        seen_axes.append(self)
        return artist

    fc.place_movable_labels = spy_place
    matplotlib.axes.Axes.annotate = spy_ann
    matplotlib.axes.Axes.text = spy_txt
    try:
        png = fc.render_forecast_chart(data, "QLD1", annotations=annotations)
    finally:
        fc.place_movable_labels = real_place
        matplotlib.axes.Axes.annotate = real_ann
        matplotlib.axes.Axes.text = real_txt

    assert png, "render returned no bytes"
    live = [ax for ax in seen_axes if ax.figure is not None]
    assert live, "no text was drawn, so there is nothing to measure"
    return fc.text_collision_pairs(live[0].figure), report


def render_and_get_figure(data, annotations=None):
    """Render and return the drawn figure, already laid out for measurement."""
    seen_axes = []
    real_ann = matplotlib.axes.Axes.annotate
    real_txt = matplotlib.axes.Axes.text

    def spy_ann(self, *args, **kwargs):
        artist = real_ann(self, *args, **kwargs)
        seen_axes.append(self)
        return artist

    def spy_txt(self, *args, **kwargs):
        artist = real_txt(self, *args, **kwargs)
        seen_axes.append(self)
        return artist

    matplotlib.axes.Axes.annotate = spy_ann
    matplotlib.axes.Axes.text = spy_txt
    try:
        fc.render_forecast_chart(data, "QLD1", annotations=annotations)
    finally:
        matplotlib.axes.Axes.annotate = real_ann
        matplotlib.axes.Axes.text = real_txt
    live = [ax for ax in seen_axes if ax.figure is not None]
    assert live, "no text was drawn, so there is nothing to measure"
    fig = live[0].figure
    fig.canvas.draw()
    return fig


def managed_label_collisions(hits):
    """Drop the pairs the chart code does not control, keep everything else.

    Only tick against tick is dropped. Anything involving a label this module
    places, the title, an axis label or the legend is kept.
    """
    return [
        h for h in hits
        if not (_is_tick(h[0]) and _is_tick(h[2]))
    ]


def test_the_measurement_itself_sees_the_legend_and_both_axes():
    """Guard the sweep before trusting it, since scope is how this defect hid.

    If ``all_text_items`` quietly stopped reporting the legend frame or the
    right hand axis, every assertion below would pass while saying nothing. So
    assert the inventory contains the kinds the issue turned on.
    """
    fig = render_and_get_figure(maintainer_style_fixture())
    items = fc.all_text_items(fig, fig.canvas.get_renderer())
    kinds = {k for k, _t, _r in items}
    for required in ("legend0", "title0", "xtick0", "ytick0", "ytick1"):
        assert required in kinds, f"{required} missing from {sorted(kinds)}"
    assert len(items) > 40, f"only {len(items)} text items found"
    print(f"  PASS: sweep inventory covers {len(kinds)} kinds, "
          f"{len(items)} text items, including the legend and both axes")


def test_daily_maximum_no_longer_prints_across_the_legend():
    """The maintainer's reported case, pinned.

    On main the Mon 7 Sep maximum overlaps the legend frame by about 39 x 10 px,
    landing across a legend row, and does so with the callouts present and with
    them absent. Nothing that overlaps the legend is acceptable here.
    """
    for with_spikes in (True, False):
        for anns in (None, [lor2_annotation()]):
            hits, _report = render_and_measure(
                maintainer_style_fixture(with_spikes=with_spikes),
                annotations=anns,
            )
            on_legend = [h for h in hits
                         if h[0].startswith("legend") or h[2].startswith("legend")]
            assert on_legend == [], (
                f"spikes={with_spikes} notices={anns is not None}: {on_legend}"
            )
    print("  PASS: nothing overlaps the legend on the maintainer style fixture, "
          "with and without spikes, with and without notices")


def test_no_managed_label_collides_on_the_maintainer_style_fixture():
    """The whole figure, not a subset: only tick against tick may remain."""
    for with_spikes in (True, False):
        for anns in (None, [lor2_annotation()]):
            hits, _report = render_and_measure(
                maintainer_style_fixture(with_spikes=with_spikes),
                annotations=anns,
            )
            bad = managed_label_collisions(hits)
            assert bad == [], (
                f"spikes={with_spikes} notices={anns is not None}: {bad}"
            )
    print("  PASS: no managed label collides with anything on the maintainer "
          "style fixture")


def test_no_managed_label_collides_across_the_price_and_floor_sweep():
    """Sweep the things that move these labels: price level, p10 floor, seed.

    The price level sets where the daily extremes sit relative to the legend and
    the clip line. The p10 floor sets the axis span, which is what moved the
    collision from one legend row to another. The seed moves which interval of
    each day is the extreme, which is what decides whether the last day's label
    reaches the right hand ``$/MWh`` tick gutter.
    """
    checked = 0
    for peak in (0.05, 0.2, 0.9):
        for floor in (0.0, -0.04, -1.0, -3.0):
            for seed in (1, 2, 3, 4, 5):
                data = diurnal_chart(peak, floor, seed)
                hits, _report = render_and_measure(data)
                bad = managed_label_collisions(hits)
                assert bad == [], (
                    f"peak={peak} floor={floor} seed={seed}: {bad}"
                )
                checked += 1
    assert checked >= 60, f"only {checked} charts swept"
    print(f"  PASS: {checked} charts swept across price level, p10 floor and "
          "seed, zero managed label collisions")


def test_nothing_overhangs_the_right_hand_axis_tick_labels():
    """The 40 of 60 manifestation from the issue, stated as its own case.

    A daily extreme in the last hours of the chart used to be centred on its
    marker regardless of how close the marker was to the right spine, so the
    label ran out over the ``$/MWh`` numbers. Inside the axes is now a hard
    requirement of the placement, which is what removes this.
    """
    offenders = []
    for peak in (0.05, 0.2, 0.9):
        for seed in (1, 2, 3, 4, 5):
            hits, _report = render_and_measure(diurnal_chart(peak, -0.04, seed))
            for h in hits:
                pair = (h[0], h[2])
                if any(k.startswith("ytick") for k in pair) and not (
                    _is_tick(h[0]) and _is_tick(h[2])
                ):
                    offenders.append((peak, seed, h))
    assert offenders == [], offenders
    print("  PASS: no label overhangs either y axis tick column across 15 charts")


def test_the_day_divider_label_stays_inside_the_axes():
    """Inside the axes is a hard constraint, so state it separately.

    The divider labels sit at the bottom of the plot and used to dip into the x
    tick labels below the spine on short charts. The placement rejects any
    candidate whose box is not wholly inside the axes, so a divider label that
    cannot fit is nudged rather than allowed to leave.
    """
    checked = 0
    for n in (12, 48, 96, 336):
        for floor in (0.0, -3.0):
            data = diurnal_chart(0.2, floor, 7, n=n)
            fig = render_and_get_figure(data)
            renderer = fig.canvas.get_renderer()
            ax = fig.axes[0]
            ab = ax.get_window_extent(renderer)
            for _kind, text, rect in fc.all_text_items(fig, renderer):
                if not _DIVIDER_TEXT.match(text):
                    continue
                checked += 1
                assert (
                    rect[0] >= ab.x0 - 0.5 and rect[2] <= ab.x1 + 0.5
                    and rect[1] >= ab.y0 - 0.5 and rect[3] <= ab.y1 + 0.5
                ), f"n={n} floor={floor}: divider label {text!r} at {rect} " \
                   f"leaves the axes {(ab.x0, ab.y0, ab.x1, ab.y1)}"
    assert checked >= 8, f"only {checked} divider labels checked"
    print(f"  PASS: {checked} day divider labels all inside the axes across "
          "chart widths 12 to 336 intervals")


def test_placement_reports_a_mode_for_every_label_and_degrades_honestly():
    """The report is the honesty mechanism, so pin its shape and its values.

    ``place_movable_labels`` returns (label text, mode) per label. ``clear`` means
    it found a free position. ``least_overlap`` means nothing was free and it
    took the least bad position, which the module logs at debug level.
    ``dropped`` means a daily extreme was removed entirely, which is only safe
    because the marker dot stays and so nothing false is printed. On the
    fixtures below every label is placed clear, and the assertion is that no
    mode outside the known set ever appears.
    """
    known = {"clear", "least_overlap", "dropped", "default"}
    total = 0
    modes = {}
    cases = [(maintainer_style_fixture(), [lor2_annotation()])]
    for peak in (0.05, 0.9):
        for floor in (0.0, -3.0):
            cases.append((diurnal_chart(peak, floor, 11), None))
    for data, anns in cases:
        _hits, report = render_and_measure(data, annotations=anns)
        assert report, "no labels were placed, so the report proves nothing"
        for label, mode in report:
            assert mode in known, f"unknown placement mode {mode!r}"
            assert isinstance(label, str) and label, "report entry has no text"
            modes[mode] = modes.get(mode, 0) + 1
            total += 1
    assert total >= 40, f"only {total} labels placed across {len(cases)} charts"
    assert modes.get("clear", 0) == total, (
        f"expected every label placed clear on these fixtures, got {modes}"
    )
    print(f"  PASS: {total} labels placed across {len(cases)} charts, modes "
          f"{modes}, no label needed a degraded position")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL: {name}: {exc}")
    print("FAILURES:", failures)
    sys.exit(1 if failures else 0)
