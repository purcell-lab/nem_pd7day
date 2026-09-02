"""Guard: no em dash and no en dash in any text the charts draw.

Chart PNGs are shown on the dashboard, so every string they draw is user
facing output and the house rule against em dashes and en dashes applies to
it. Both checks below are written to catch a new occurrence anywhere in the
chart code rather than only at the sites fixed for issue #92.

Check 1 is static: parse every module in the integration that imports
matplotlib and reject the two characters in any string literal that is not a
docstring. That covers escaped forms such as the six digit backslash-u
escapes the code used, because the parser resolves them, and it covers
literals nobody has written a render fixture for yet.

Check 2 is dynamic: instrument matplotlib.text.Text.set_text, render every
chart, and inspect the strings that actually reached the renderer.
That covers labels built at runtime from parts, which a literal scan cannot
see; the horizon tick labels in bias_chart are assembled from a bucket key
and a separator, and that separator was one of the offenders.
"""
from __future__ import annotations

import ast
import io
import os
from datetime import datetime, timedelta, timezone

import pytest

NEM = timezone(timedelta(hours=10))
BAD = {"\u2014": "em dash", "\u2013": "en dash"}

_PKG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "nem_pd7day",
)


def _chart_modules() -> list[str]:
    """Every module in the package that imports matplotlib, discovered, not listed.

    Discovery rather than a fixed list is the point: a chart module added
    later is guarded without anyone remembering to extend this test.
    """
    found = []
    for name in sorted(os.listdir(_PKG)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(_PKG, name), encoding="utf-8") as handle:
            if "matplotlib" in handle.read():
                found.append(name[:-3])
    return found


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


def _literal_offenders(module_name: str) -> list[str]:
    path = os.path.join(_PKG, f"{module_name}.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    skip = _docstring_nodes(tree)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        for ch, label in BAD.items():
            if ch in node.value:
                offenders.append(f"{module_name}.py line {node.lineno}: {label} in {node.value!r}")
    return offenders


def test_chart_modules_are_discovered():
    """The discovery must actually find the chart modules, or check 1 is vacuous."""
    mods = _chart_modules()
    for expected in ("forecast_chart", "iso_chart", "bias_chart"):
        assert expected in mods, f"{expected} not discovered, guard would be vacuous"
    print(f"  PASS: chart module discovery found {len(mods)}: {', '.join(mods)}")


@pytest.mark.parametrize("module_name", _chart_modules())
def test_no_dash_in_chart_string_literals(module_name):
    """No em dash or en dash in any non-docstring string literal of a chart module."""
    offenders = _literal_offenders(module_name)
    assert not offenders, "dash characters in chart text: " + "; ".join(offenders)
    print(f"  PASS: no em dash or en dash in {module_name}.py string literals")


# ── Fixtures for the render side ───────────────────────────────────────────────

def _calibration_result():
    """A CalibrationResult with fitted cells in every horizon bucket.

    Fitted cells matter here: the bias chart bar labels are built from the
    bucket key at render time, and a bucket under MIN_OBS draws no bar, so a
    thin fixture would skip exactly the label that had to be fixed.
    """
    from custom_components.nem_pd7day.calibration_engine import (
        CalibrationResult, BucketModel, LinearCoeff, QuantileCoeff,
    )

    models = {}
    horizons = ["h00_06", "h06_12", "h12_24", "h24_48", "h48_96", "h96plus"]
    tods = ["shoulder", "morning_ramp", "solar", "peak"]
    for i, hor in enumerate(horizons):
        for j, tod in enumerate(tods):
            key = f"{hor}__{tod}"
            a = 0.7 + 0.1 * ((i + j) % 6)
            models[key] = BucketModel(
                key,
                LinearCoeff(a=a, b=0.01, n=60 + i * 5 + j, mae=0.01, rmse=0.012),
                QuantileCoeff(0.1, a=a * 0.9, b=0.008, n=60),
                QuantileCoeff(0.5, a=a, b=0.010, n=60),
                QuantileCoeff(0.9, a=a * 1.1, b=0.012, n=60),
            )
    return CalibrationResult(
        fitted_at="2026-05-01T18:00:00+10:00",
        total_observations=500,
        models=models,
    )


def _tod_stats():
    from custom_components.nem_pd7day.tod_stats import compute

    obs = []
    for day in range(5):
        for hour in range(0, 24, 2):
            for minute in (0, 30):
                obs.append({
                    "interval_time": f"2026-04-{18 + day:02d}T{hour:02d}:{minute:02d}:00+10:00",
                    "actual_rrp": 0.04 + hour * 0.004,
                    "pd7day_forecast": 0.05 + hour * 0.004,
                })
    return compute(obs)


def _forecast_rows():
    run = datetime(2026, 5, 1, 4, 0, tzinfo=NEM)
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
    import types

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


def _rendered_strings() -> list[str]:
    """Render all three charts and return every string handed to matplotlib.

    Text.__init__ funnels through set_text, so titles, legend labels, tick
    labels and annotations all pass through this one hook whichever axes API
    created them.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.text as mtext

    from custom_components.nem_pd7day import bias_chart, iso_chart, tod_stats
    from custom_components.nem_pd7day.forecast_chart import render_forecast_chart

    seen: list[str] = []
    original = mtext.Text.set_text

    def recording(self, s):
        if isinstance(s, str):
            seen.append(s)
        return original(self, s)

    result = _calibration_result()
    stats = _tod_stats()
    rows, run = _forecast_rows()

    mtext.Text.set_text = recording
    try:
        png = render_forecast_chart(rows, "QLD1", annotations=_notices(run))
        assert png[:4] == b"\x89PNG"
        png = bias_chart.render_chart(result, obs_count=500, region="QLD1")
        assert png[:4] == b"\x89PNG"
        png = bias_chart.render_chart(result, obs_count=500, region="QLD1",
                                      tod_stats=stats)
        assert png[:4] == b"\x89PNG"
        png = iso_chart.render_iso_chart(result, iso_history=[], obs_count=500,
                                         region="QLD1")
        assert png[:4] == b"\x89PNG"
        png = tod_stats.render_chart(stats, region="QLD1")
        assert png[:4] == b"\x89PNG"
    finally:
        mtext.Text.set_text = original
    return seen


def test_no_dash_in_rendered_chart_text():
    """Nothing drawn on any of the four charts contains an em dash or en dash."""
    seen = _rendered_strings()
    assert len(seen) > 50, f"only {len(seen)} strings captured, fixture is too thin"
    offenders = sorted({s for s in seen if any(ch in s for ch in BAD)})
    assert not offenders, f"dash characters reached the renderer: {offenders}"
    print(f"  PASS: {len(seen)} strings reached the renderer, none with a dash")


def test_render_capture_would_notice_a_dash():
    """The capture hook is live, proven by planting a dash through the same hook.

    Without this, a broken hook would make the render check pass silently.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.figure as mplfig
    import matplotlib.text as mtext
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    seen: list[str] = []
    original = mtext.Text.set_text

    def recording(self, s):
        if isinstance(s, str):
            seen.append(s)
        return original(self, s)

    mtext.Text.set_text = recording
    try:
        fig = mplfig.Figure()
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        ax.set_title("planted \u2014 dash")
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
    finally:
        mtext.Text.set_text = original

    assert any("\u2014" in s for s in seen), "capture hook missed a planted em dash"
    print("  PASS: render capture hook detects a planted em dash")


if __name__ == "__main__":
    import sys

    # pytest gets the repo root from conftest; the standalone runner does not.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    test_chart_modules_are_discovered()
    for mod in _chart_modules():
        test_no_dash_in_chart_string_literals(mod)
    test_no_dash_in_rendered_chart_text()
    test_render_capture_would_notice_a_dash()
    print("All chart dash guard tests passed")
