"""The chart's spike callouts, issue #84.

``camera.py`` never wrote ``spike_credible`` into the entries it hands the
chart renderer. ``_save_spike_intervals`` filters on that key, so the set it
accumulated was always empty, ``spike_first_run`` was therefore always True,
and ``forecast_chart.render_forecast_chart`` skips any interval whose
``spike_credible`` is not True before it reaches ``_is_spike_callout_eligible``.
No spike callout had ever been drawn on a chart since the code was written.

The wiring is one line. The rest of this file exists because switching the
callouts on is a visible change to the image a user looks at, and the drawing
code behind them had never executed with a non-empty credible set. Rendering
charts with one showed three defects that could not have been noticed before:

* the callout boxes were tiered by cluster index on a two value cycle, so the
  first and third clusters always shared a tier however close together they
  were, and nine callouts on a synthetic run produced 21 overlapping pairs;
* the boxes are offset a fixed number of points above the clip line while the
  y span is not fixed, so on a run whose p10 reaches the -$1000/MWh market
  floor the box was drawn 55 px above the top of the axes, over the title;
* the label reported the calibrated value, so a $12.00/kWh raw spike was
  annotated "$0.18/kWh", a number the calibrated line already draws and which
  says nothing about the spike being called out.

The tri-state matters here and is pinned below. ``apply_to_price`` sets
``spike_credible`` to True when both the gas and QNI covariates support the
spike, to None when either is missing, and does not set the key at all below
SPIKE_THRESHOLD. None and absent are not confirmed negatives, they are an
unanswered and an unasked question, and neither may be recorded as False.

Run with:  python -m pytest tests/test_camera_spike_callouts.py -v
or simply: python tests/test_camera_spike_callouts.py
"""
from __future__ import annotations

import datetime
import enum as _enum
import importlib.util
import os
import random
import sys
import types
from dataclasses import dataclass
from datetime import timedelta, timezone
from unittest.mock import MagicMock

import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.axes  # noqa: E402
import matplotlib.text  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from custom_components.nem_pd7day import forecast_chart as fc  # noqa: E402
from custom_components.nem_pd7day.calibration_engine import (  # noqa: E402
    SPIKE_THRESHOLD,
    CalibrationEngine,
    Observation,
)
from custom_components.nem_pd7day.calibration_store import CalibrationStore  # noqa: E402
from custom_components.nem_pd7day.const import (  # noqa: E402
    SPIKE_GAS_THRESHOLD_TJ,
    SPIKE_QNI_THRESHOLD_MW,
)

NEM_TZ = timezone(timedelta(hours=10))
RUN_DT = datetime.datetime.now(NEM_TZ).replace(
    hour=4, minute=0, second=0, microsecond=0
) - timedelta(days=1)


def _load_camera_module():
    """Import camera.py, stubbing the Home Assistant camera platform if needed.

    Several test modules install MagicMock stand ins for the ``homeassistant``
    package at import time and whichever runs first wins for the session, so a
    plain import of camera.py passes standalone and fails in a full suite run.
    This mirrors tests/test_camera_setup.py and tests/test_camera_calibration_parity.py.
    """
    try:
        from custom_components.nem_pd7day.camera import (
            NemPd7dayForecastChartCamera as _cls,
        )
        return sys.modules[_cls.__module__]
    except (ImportError, AttributeError, TypeError):
        pass

    class _CameraEntityFeature(_enum.IntFlag):
        NONE = 0

    class _FakeCamera:
        def __init__(self) -> None:
            self._removals: list = []

        def async_on_remove(self, func) -> None:
            self._removals.append(func)

    class _FakeCoordinatorEntity:
        def __init__(self, coordinator=None, **kwargs):
            self.coordinator = coordinator

        def __class_getitem__(cls, item):
            return cls

    camera_stub = MagicMock()
    camera_stub.Camera = _FakeCamera
    camera_stub.CameraEntityFeature = _CameraEntityFeature
    sys.modules["homeassistant.components.camera"] = camera_stub

    uc_stub = MagicMock()
    uc_stub.CoordinatorEntity = _FakeCoordinatorEntity
    uc_stub.DataUpdateCoordinator = MagicMock()
    uc_stub.UpdateFailed = Exception
    sys.modules["homeassistant.helpers.update_coordinator"] = uc_stub

    name = "custom_components.nem_pd7day.camera"
    path = os.path.join(_ROOT, "custom_components", "nem_pd7day", "camera.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_camera_mod = _load_camera_module()
NemPd7dayForecastChartCamera = _camera_mod.NemPd7dayForecastChartCamera


def nem_iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


# Camera side fixture


@dataclass
class FakePeriod:
    """A PricePeriod: ``time`` is the interval START, ``nemtime`` the END."""

    time: str
    nemtime: str
    value: float


@dataclass
class FakeFlow:
    time: str
    mwflow: float


@dataclass
class FakeGas:
    nemtime: str
    value_tj: float


class FakeCoordinator:
    """Coordinator stand-in exposing what calibration_inputs actually reads."""

    def __init__(self, periods, gas_tj, qni_mw):
        price_data = types.SimpleNamespace(
            forecast=list(periods), forecast_generated_at=nem_iso(RUN_DT)
        )
        flows = (
            [FakeFlow(time=p.time, mwflow=qni_mw) for p in periods]
            if qni_mw is not None
            else []
        )
        gas = (
            types.SimpleNamespace(
                forecast=[
                    FakeGas(nemtime=p.nemtime, value_tj=gas_tj) for p in periods
                ]
            )
            if gas_tj is not None
            else None
        )
        self.data = types.SimpleNamespace(
            prices={"QLD1": price_data},
            interconnectors={"NSW1-QLD1": types.SimpleNamespace(forecast=flows)},
            market_summary=gas,
        )
        self.last_update_success = True
        self._store = None

    def stpasa_index(self):
        return None, {}, []

    @property
    def current_run_features(self):
        return None


def fitted_store() -> CalibrationStore:
    """A CalibrationStore holding a real fit.

    A store with no calibration at all returns early from ``apply_to_price``
    and never reaches the spike annotation, so the fit has to be real. It does
    not have to be a good one: these tests are about which fields travel, not
    about the numbers, so a handful of observations that leave the buckets
    under MIN_OBS and take the passthrough branch is enough.
    """
    rng = random.Random(5)
    engine = CalibrationEngine()
    train_run_at = nem_iso(RUN_DT - timedelta(days=20))
    observations = []
    for j in range(12):
        dt = (RUN_DT - timedelta(days=20)).replace(hour=(4 + j) % 24, minute=0)
        observations.append(
            Observation(
                interval_time=nem_iso(dt),
                horizon_hours=2.0 + j,
                pd7day_forecast=rng.uniform(0.05, 0.25),
                actual_rrp=rng.uniform(0.05, 0.30),
                forecast_run_at=train_run_at,
                hour_of_day=dt.hour,
                day_of_week=dt.weekday(),
                month=dt.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )
    store = CalibrationStore(MagicMock(), "QLD1")
    store._calibration = engine.fit(observations)
    store._fit_generation = 1
    return store


def make_camera(values, gas_tj=200.0, qni_mw=-500.0):
    """A forecast chart camera over a run whose prices are ``values``."""
    periods = []
    for i, value in enumerate(values):
        start = RUN_DT + timedelta(minutes=30 * (i + 1))
        periods.append(
            FakePeriod(
                time=nem_iso(start),
                nemtime=nem_iso(start + timedelta(minutes=30)),
                value=value,
            )
        )
    store = fitted_store()
    coordinator = FakeCoordinator(periods, gas_tj, qni_mw)
    coordinator._store = store

    entry = MagicMock()
    entry.entry_id = "entry_spike_callouts"
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, store=store, dispatch=None
    )

    camera = NemPd7dayForecastChartCamera.__new__(NemPd7dayForecastChartCamera)
    camera.coordinator = coordinator
    camera._region = "QLD1"
    camera._entry = entry
    camera._image_bytes = b""
    camera._attr_unique_id = "entry_spike_callouts_QLD1_forecast_chart"
    camera.hass = MagicMock()
    return camera


# Chart side fixture


def chart_entry(i, raw, calibrated=0.18, p10=None, credible="omit", first_run=False):
    start = RUN_DT + timedelta(minutes=30 * (i + 1))
    entry = {
        "nemtime": nem_iso(start + timedelta(minutes=30)),
        "time": nem_iso(start),
        "raw_value": raw,
        "calibrated": calibrated,
        "p10": calibrated * 0.9 if p10 is None else p10,
        "p50": calibrated,
        "p90": calibrated * 1.1,
        "calibrated_source": "isotonic",
        "horizon_hours": round((i + 1) * 0.5, 1),
        "forecast_run_at": nem_iso(RUN_DT),
        "spike_first_run": first_run,
    }
    if credible != "omit":
        entry["spike_credible"] = credible
    return entry


def render_and_collect(data, annotations=None):
    """Render a chart and return the placed callout boxes and the axes box.

    ``get_window_extent`` on an Annotation covers the arrow as well as the
    text, so the label's own bounding box is taken from its bbox patch.
    """
    calls = []
    real_annotate = matplotlib.axes.Axes.annotate

    def spy(self, text, *args, **kwargs):
        artist = real_annotate(self, text, *args, **kwargs)
        if str(text).startswith("raw $"):
            calls.append((self, artist))
        return artist

    matplotlib.axes.Axes.annotate = spy
    try:
        png = fc.render_forecast_chart(data, "QLD1", annotations=annotations)
    finally:
        matplotlib.axes.Axes.annotate = real_annotate

    boxes, labels, axes_box = [], [], None
    for ax, artist in calls:
        # render_forecast_chart saves at dpi 110 with bbox_inches tight, which
        # leaves the artists laid out for that dpi while ax.get_window_extent
        # reports in figure dpi. Redraw on the figure's own canvas first so
        # every extent below is measured in one coordinate system.
        ax.figure.canvas.draw()
        renderer = ax.figure.canvas.get_renderer()
        patch = artist.get_bbox_patch()
        boxes.append(
            patch.get_window_extent(renderer)
            if patch is not None
            else artist.get_window_extent(renderer)
        )
        labels.append(artist.get_text())
        axes_box = ax.get_window_extent(renderer)
    return png, boxes, labels, axes_box


def overlapping_pairs(boxes):
    out = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1:
                out.append((i, j))
    return out


def outside(boxes, axes_box):
    if axes_box is None:
        return []
    return [
        i
        for i, b in enumerate(boxes)
        if b.y1 > axes_box.y1
        or b.y0 < axes_box.y0
        or b.x0 < axes_box.x0
        or b.x1 > axes_box.x1
    ]


_NOTICE_LABELS = {"LOR1", "LOR2", "LOR3", "MSL1", "MSL2", "MSL3"}


def _strip_family(text):
    """Name the label family, or None for text that does not share the strip.

    Everything above the clip line competes for the same thin band: the clip
    line label, the per day extreme labels, the grid stress notice labels and
    the spike callout boxes. Axis tick labels and the day divider labels are
    deliberately excluded. Those two collide with each other on main on most
    charts, with or without callouts, and that is a separate pre-existing
    defect rather than anything this change touches.
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


def collect_strip_text(data, annotations=None):
    """Return (family, text, box) for every label in the above clip line strip.

    Annotation.get_window_extent covers the leader arrow as well as the text,
    which would report a collision against everything the arrow passes over, so
    a label with a bbox patch is measured from the patch and one without is
    measured as plain text.
    """
    calls = []
    real_annotate = matplotlib.axes.Axes.annotate
    real_text = matplotlib.axes.Axes.text

    def spy_annotate(self, text, *args, **kwargs):
        artist = real_annotate(self, text, *args, **kwargs)
        calls.append((self, artist))
        return artist

    def spy_text(self, x, y, text, *args, **kwargs):
        artist = real_text(self, x, y, text, *args, **kwargs)
        calls.append((self, artist))
        return artist

    matplotlib.axes.Axes.annotate = spy_annotate
    matplotlib.axes.Axes.text = spy_text
    try:
        fc.render_forecast_chart(data, "QLD1", annotations=annotations)
    finally:
        matplotlib.axes.Axes.annotate = real_annotate
        matplotlib.axes.Axes.text = real_text

    out = []
    drawn = set()
    for ax, artist in calls:
        label = artist.get_text()
        family = _strip_family(label)
        if family is None:
            continue
        if id(ax.figure) not in drawn:
            # One draw per figure, not per artist. render_forecast_chart saves
            # at dpi 110 with bbox_inches tight, so the artists are left laid
            # out for that dpi while get_window_extent reports in figure dpi;
            # redrawing on the figure's own canvas puts every extent below into
            # one coordinate system.
            ax.figure.canvas.draw()
            drawn.add(id(ax.figure))
        renderer = ax.figure.canvas.get_renderer()
        patch = artist.get_bbox_patch() if hasattr(artist, "get_bbox_patch") else None
        if patch is not None:
            box = patch.get_window_extent(renderer)
        else:
            box = matplotlib.text.Text.get_window_extent(artist, renderer)
        out.append((family, label, box))
    return out


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


# Camera tests


def test_camera_carries_spike_credible_into_the_chart_entry():
    """The bare claim of issue #84. This fails on main, where the key is absent."""
    camera = make_camera([0.10, 12.0, 0.08])
    entries = camera._build_forecast_data()
    spike = [e for e in entries if e["raw_value"] >= SPIKE_THRESHOLD]
    assert spike, "fixture produced no spike interval"
    assert "spike_credible" in spike[0], (
        "camera dropped spike_credible, so the chart can never draw a callout: "
        f"keys={sorted(spike[0])}"
    )
    assert spike[0]["spike_credible"] is True
    print("  PASS: camera carries spike_credible into the chart entry")


def test_covariate_gate_is_what_decides_credibility():
    """The gate is gas above 150 TJ and QNI below -300 MW, both together."""
    cases = {
        (200.0, -500.0): True,
        (100.0, -500.0): False,
        (200.0, -100.0): False,
        (100.0, -100.0): False,
    }
    assert SPIKE_GAS_THRESHOLD_TJ == 150.0
    assert SPIKE_QNI_THRESHOLD_MW == -300.0
    for (gas, qni), expected in cases.items():
        camera = make_camera([12.0], gas_tj=gas, qni_mw=qni)
        entry = camera._build_forecast_data()[0]
        assert entry["spike_credible"] is expected, (
            f"gas={gas} qni={qni} gave {entry['spike_credible']!r}, "
            f"expected {expected!r}"
        )
    print("  PASS: the gas and QNI gate decides spike_credible")


def test_missing_covariate_is_none_and_never_false():
    """An unanswered question is not a confirmed negative.

    ``apply_to_price`` sets None when either covariate is missing. Reading that
    as False would say the market data ruled the spike out when in fact it was
    never consulted.
    """
    for gas, qni in ((None, -500.0), (200.0, None), (None, None)):
        camera = make_camera([12.0], gas_tj=gas, qni_mw=qni)
        entry = camera._build_forecast_data()[0]
        assert "spike_credible" in entry
        assert entry["spike_credible"] is None, (
            f"gas={gas} qni={qni} gave {entry['spike_credible']!r}, expected None"
        )
    print("  PASS: a missing covariate carries through as None, not False")


def test_key_is_absent_below_the_spike_threshold():
    """Below SPIKE_THRESHOLD the question is never asked, so no key is written.

    Absent, None and True are three different facts and the camera keeps them
    apart. A default of False here would be the missing-data-as-zero mistake in
    another dress.
    """
    camera = make_camera([0.10, 2.99, 3.00])
    entries = camera._build_forecast_data()
    assert "spike_credible" not in entries[0]
    assert "spike_credible" not in entries[1]
    assert entries[2]["spike_credible"] is True
    print("  PASS: spike_credible is absent below the threshold, not False")


def test_the_spike_interval_set_is_no_longer_always_empty():
    """``_save_spike_intervals`` accumulated nothing before this change."""
    camera = make_camera([0.10, 12.0, 0.08, 9.0])
    camera._build_forecast_data()
    saved = camera._prior_spike_intervals()
    assert len(saved) == 2, f"expected both spike intervals to be saved, got {saved}"
    print("  PASS: the saved spike interval set is populated")


def test_spike_first_run_now_distinguishes_a_repeat_from_a_new_spike():
    """The persistence scoring the field exists for had never distinguished anything.

    With the prior set always empty, ``spike_first_run`` was True for every
    interval of every run, so ``_is_spike_callout_eligible`` could only ever
    have returned "candidate". A confirmed spike was unreachable twice over.
    """
    camera = make_camera([0.10, 12.0, 0.08])
    first = camera._build_forecast_data()
    assert [e["spike_first_run"] for e in first] == [True, True, True]
    second = camera._build_forecast_data()
    flags = {e["time"]: e["spike_first_run"] for e in second}
    spike_time = first[1]["time"]
    assert flags[spike_time] is False, (
        "a spike seen in the previous run is still being reported as first run"
    )
    assert flags[first[0]["time"]] is True
    print("  PASS: spike_first_run distinguishes a repeat from a new spike")


def test_an_uncredible_spike_is_not_saved_as_a_prior_spike():
    """Only True belongs in the set, so None and False stay out of it."""
    for gas, qni in ((None, None), (100.0, -100.0)):
        camera = make_camera([12.0], gas_tj=gas, qni_mw=qni)
        camera._build_forecast_data()
        assert camera._prior_spike_intervals() == set()
    print("  PASS: only a credible spike enters the prior spike set")


# Chart rendering tests


def test_callout_reports_the_raw_spike_not_the_calibrated_value():
    """A spike callout that quotes the calibrated value says nothing.

    Before this change the label was the cluster's maximum calibrated value, so
    a $12.00/kWh raw forecast was annotated with a number near the clip line
    that the calibrated line already draws.
    """
    data = [chart_entry(i, 0.05) for i in range(96)]
    data[20] = chart_entry(20, 12.0, credible=True)
    _png, boxes, labels, _axes = render_and_collect(data)
    assert len(boxes) == 1, f"expected one callout, got {len(boxes)}"
    assert "12.00" in labels[0], f"callout label was {labels[0]!r}"
    print("  PASS: the callout reports the raw spike value")


def test_a_non_true_spike_credible_draws_no_callout():
    """None, False and absent all draw nothing. Only True is a confirmed spike."""
    for credible in (None, False, "omit"):
        data = [chart_entry(i, 0.05) for i in range(96)]
        data[20] = chart_entry(20, 12.0, credible=credible)
        _png, boxes, _labels, _axes = render_and_collect(data)
        assert boxes == [], f"spike_credible={credible!r} drew {len(boxes)} callouts"
    print("  PASS: only a True spike_credible draws a callout")


def test_a_first_run_spike_draws_a_marker_but_no_callout_box():
    """Persistence styling: a spike seen once is a candidate, not a confirmation.

    Worth stating plainly because it means the first chart rendered after this
    change lands carries grey triangles and no boxes at all.
    """
    data = [chart_entry(i, 0.05) for i in range(96)]
    data[20] = chart_entry(20, 12.0, credible=True, first_run=True)
    _png, boxes, _labels, _axes = render_and_collect(data)
    assert boxes == []
    print("  PASS: a first run spike draws no callout box")


def test_adjacent_spikes_do_not_produce_overlapping_callouts():
    """The regression case. Clusters were tiered on a two value cycle by index.

    Four spike episodes a couple of hours apart put clusters 0 and 2 on the
    same tier and clusters 1 and 3 on the other, and on main all six pairs of
    boxes overlap.
    """
    data = [chart_entry(i, 0.05) for i in range(96)]
    for i in (10, 14, 18, 22):
        data[i] = chart_entry(i, 12.0 + i, credible=True)
    _png, boxes, _labels, axes_box = render_and_collect(data)
    assert boxes, "no callouts drawn, so the assertion below would be vacuous"
    assert overlapping_pairs(boxes) == [], (
        f"{len(boxes)} callouts produced overlapping boxes: {overlapping_pairs(boxes)}"
    )
    assert outside(boxes, axes_box) == []
    print("  PASS: adjacent spike episodes do not overlap")


def test_callout_stays_inside_the_axes_when_p10_reaches_the_market_floor():
    """The boxes are offset in points and the y span is not fixed.

    A p10 at the -$1000/MWh floor stretches the axis until the fixed offset
    lands outside it. On main the box is drawn 55 px above the axes top, where
    it is painted over the chart title.
    """
    for floor in (-0.04, -0.30, -1.00, -3.00):
        data = [chart_entry(i, 0.05) for i in range(96)]
        data[5] = chart_entry(5, 0.05, p10=floor)
        data[20] = chart_entry(20, 12.0, credible=True)
        _png, boxes, _labels, axes_box = render_and_collect(data)
        assert len(boxes) == 1
        assert outside(boxes, axes_box) == [], (
            f"p10 floor {floor} put the callout outside the axes: "
            f"box={boxes[0]} axes={axes_box}"
        )
    print("  PASS: the callout stays inside the axes at any p10 floor")


def test_callout_does_not_land_on_a_grid_stress_label():
    """LOR and MSL labels sit in the same strip of chart above the clip line.

    The notice labels now live in a band of their own below the callout tiers,
    so the two cannot collide vertically at all. The horizontal seeding of the
    tier search is still there as a second line of defence.
    """
    data = [chart_entry(i, 0.05) for i in range(96)]
    data[20] = chart_entry(20, 12.0, credible=True)
    ann = types.SimpleNamespace(
        is_cancelled=False,
        notice_type="LOR",
        level=2,
        period_from=RUN_DT + timedelta(minutes=30 * 19),
        period_to=RUN_DT + timedelta(minutes=30 * 23),
        notice_id="n1",
    )
    _png, boxes, _labels, axes_box = render_and_collect(data, annotations=[ann])
    assert len(boxes) == 1
    assert outside(boxes, axes_box) == []
    items = collect_strip_text(data, annotations=[ann])
    assert any(f == "notice" for f, _t, _b in items), (
        "the LOR2 label was not drawn, so this test proves nothing"
    )
    assert any(f == "callout" for f, _t, _b in items), "no callout drawn"
    assert strip_collisions(items) == [], strip_collisions(items)
    print("  PASS: the callout avoids a grid stress label")


def test_callout_layout_sweep():
    """Invariant sweep: no callout ever overlaps another or leaves the axes.

    Point cases pick the shapes you thought of. This walks spike density, the
    depth of the p10 floor, the level the calibrated line sits at and the
    number of intervals, because the offending geometry here was a product of
    all four and none of them alone.
    """
    rng = random.Random(19)
    charts = drawn = 0
    for rate in (0.02, 0.20, 1.0):
        for floor in (0.02, -1.00):
            for level in (0.02, 1.00):
                for n in (1, 96, 336):
                    data = []
                    for i in range(n):
                        raw = (
                            rng.uniform(3.0, 25.0)
                            if rng.random() < rate
                            else rng.uniform(0.01, max(level, 0.02))
                        )
                        data.append(
                            chart_entry(
                                i,
                                raw,
                                calibrated=level,
                                p10=floor if i == 0 else None,
                                credible=True if raw >= SPIKE_THRESHOLD else "omit",
                            )
                        )
                    _png, boxes, _labels, axes_box = render_and_collect(data)
                    charts += 1
                    drawn += len(boxes)
                    assert overlapping_pairs(boxes) == [], (
                        f"rate={rate} floor={floor} level={level} n={n}: "
                        f"{overlapping_pairs(boxes)}"
                    )
                    assert outside(boxes, axes_box) == [], (
                        f"rate={rate} floor={floor} level={level} n={n}: "
                        f"callout outside the axes"
                    )
    assert drawn > 20, f"sweep drew only {drawn} callouts, so it proves little"
    print(f"  PASS: {charts} charts, {drawn} callouts, none overlapping or clipped")


def test_dense_spikes_do_not_bury_the_chart_in_labels():
    """Every credible spike gets a triangle, but the labels are capped.

    A run where most intervals inside the callout window are spikes is not
    something a reader can be shown 90 boxes for. Clustering and the tier
    search between them keep the label count small; the triangles carry the
    per interval detail.
    """
    data = [chart_entry(i, 12.0, credible=True) for i in range(96)]
    data += [chart_entry(i, 0.05) for i in range(96, 336)]
    _png, boxes, _labels, axes_box = render_and_collect(data)
    assert len(boxes) <= len(fc._CALLOUT_Y_OFFSETS_PT), (
        f"96 consecutive credible spikes drew {len(boxes)} callout boxes"
    )
    assert overlapping_pairs(boxes) == []
    assert outside(boxes, axes_box) == []
    print(f"  PASS: 96 consecutive credible spikes draw {len(boxes)} callout boxes")


def test_render_still_returns_a_png_with_callouts_present():
    """The path had never executed with a non-empty set, so prove it does not raise."""
    data = [chart_entry(i, 0.05) for i in range(336)]
    for i in (10, 11, 40, 41, 42, 80):
        data[i] = chart_entry(i, 12.0, credible=True)
    png, boxes, _labels, _axes = render_and_collect(data)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert boxes, "no callout drawn, so the render was not exercising the new path"
    print("  PASS: a chart with callouts renders to a valid PNG")


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


def evening_spike_fixture():
    """A seven day chart shaped like the one a reviewer looked at.

    Synthetic. It matters that this is a full 336 interval chart with a diurnal
    shape, because the clip line label is anchored to the third interval and so
    only occupies the leftmost tenth of the chart width. The label it collided
    with is the first day's maximum, which in this shape falls in the evening of
    the first day and lands inside that tenth. A flat fixture does not reproduce
    it.
    """
    import math

    spikes = {}
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
        data.append(
            chart_entry(
                i, raw, calibrated=cal,
                credible=True if raw >= SPIKE_THRESHOLD else "omit",
            )
        )
    return data


def test_the_clip_label_does_not_collide_with_a_clipped_daily_maximum():
    """The regression the headroom reservation introduced, pinned.

    Everything above the clip line used to be positioned as a fraction of
    CLIP_Y. Reserving headroom for the callout tiers raises the axis top, which
    squeezes those fractions toward the clip line and onto the daily extreme
    labels, which sit at a fixed 9 pt above their marker. On main the clip
    label and the first day's maximum clear each other by a tenth of a pixel,
    which is luck rather than design, and the headroom reservation turned that
    into a measured overlap of 3.4 px. The strip is now allocated in points and
    does not move with the y limits.
    """
    ann = types.SimpleNamespace(
        is_cancelled=False, notice_type="LOR", level=2,
        period_from=RUN_DT + timedelta(hours=13),
        period_to=RUN_DT + timedelta(hours=16), notice_id="n1",
    )
    items = collect_strip_text(evening_spike_fixture(), annotations=[ann])
    families = {f for f, _t, _b in items}
    for needed in ("clip", "extreme", "notice", "callout"):
        assert needed in families, (
            f"no {needed} label drawn, so this test proves nothing"
        )
    assert strip_collisions(items) == [], strip_collisions(items)
    print("  PASS: nothing in the clip line strip collides")


def test_no_label_in_the_clip_line_strip_collides_across_a_y_limit_sweep():
    """The y limits are what move these labels, so sweep the y limits.

    The point of this test is that it varies exactly the things that change the
    axis span: the price level, the depth of the p10 floor and whether a
    callout is present at all to trigger the headroom reservation.

    Synthetic data. It shows that the strip holds together across the span, not
    that any of these shapes occur in the market.
    """
    rng = random.Random(29)
    ann = types.SimpleNamespace(
        is_cancelled=False, notice_type="LOR", level=2,
        period_from=RUN_DT + timedelta(hours=5),
        period_to=RUN_DT + timedelta(hours=8), notice_id="n1",
    )
    checked = 0
    with_callouts = 0
    for level in (0.02, 0.18, 0.30, 1.00):
        for floor in (0.02, -0.04, -1.00, -3.00):
            for spikes in ((), (10, 11), (10, 11, 40, 41, 70)):
                data = []
                for i in range(96):
                    raw = rng.uniform(0.01, max(level, 0.02))
                    data.append(chart_entry(i, raw, calibrated=level,
                                            p10=floor if i == 0 else None))
                for i in spikes:
                    data[i] = chart_entry(i, 4.0 + i, calibrated=level,
                                          p10=floor if i == 0 else None,
                                          credible=True)
                items = collect_strip_text(data, annotations=[ann])
                if any(f == "callout" for f, _t, _b in items):
                    with_callouts += 1
                checked += 1
                assert strip_collisions(items) == [], (
                    f"level={level} floor={floor} spikes={spikes}: "
                    f"{strip_collisions(items)}"
                )
    assert checked >= 40, f"only {checked} charts swept"
    assert with_callouts >= 20, (
        f"only {with_callouts} charts drew a callout, so the headroom "
        "reservation was barely exercised"
    )
