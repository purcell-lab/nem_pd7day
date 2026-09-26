"""Golden master: every entity of every scenario, compared exactly to its snapshot.

Spec 000, part A. Each scenario in tests/golden/scenarios.py is built through
the real platform setup with the clock frozen (tests/golden/harness.py), read
the way Home Assistant reads an entity, written as canonical JSON
(tests/golden/snapshot.py) and compared with tests/golden/snapshots/<name>.json.gz
with no tolerance at all: a value that moves by one ulp fails.

Regenerate with::

    GOLDEN_UPDATE=1 python -m pytest tests/test_golden_master.py

A pull request titled "refactor:" must not change the snapshots. Any other
pull request that does must say which values move and why.

Each scenario also asserts one fact about its own output, so a scenario that
silently stopped exercising what its name says fails here rather than
passing on a snapshot of something else.
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Callable

import matplotlib
import pytest

from golden import clock, harness, snapshot
from golden.scenarios import NSW_DISPATCH_RRP, SCENARIOS

SNAPSHOT_DIR = Path(__file__).parent / "golden" / "snapshots"
UPDATE = os.environ.get("GOLDEN_UPDATE") == "1"
MAX_DIFF_LINES = 40


# ── Helpers over a snapshot ──────────────────────────────────────────────────

def _records(snap: dict, suffix: str) -> list[dict]:
    return [r for uid, r in snap["entities"].items() if uid.endswith(suffix)]


def _one(snap: dict, suffix: str) -> dict:
    found = _records(snap, suffix)
    assert len(found) == 1, f"expected one entity ending {suffix!r}, found {len(found)}"
    return found[0]


def _forecast(snap: dict) -> list[dict]:
    return list(snapshot.iter_forecast(_one(snap, "_forecast")))


# ── One non-vacuity fact per scenario ────────────────────────────────────────

def _fitted_evening_peak(built: harness.Built, snap: dict) -> None:
    forecast = _forecast(snap)
    sources = {e["calibrated_source"] for e in forecast}
    assert {"isotonic", "isotonic+stpasa"} <= sources, sources
    assert _one(snap, "_calibration")["extra_state_attributes"]["status"] == "active"
    assert built.scenario.now.hour == 18


def _days27(built: harness.Built, snap: dict) -> None:
    cutoff = "2026-09-17T03:30:00+10:00"
    day27 = list(snapshot.iter_forecast(_one(snap, "_forecast_days27")))
    tariff27 = list(snapshot.iter_forecast(_one(snap, "_6900_days27")))
    assert day27 and tariff27
    assert all(e["time"] > cutoff for e in day27 + tariff27)
    assert any(e["time"] <= cutoff for e in _forecast(snap))


def _empty_store(built: harness.Built, snap: dict) -> None:
    assert {e["calibrated_source"] for e in _forecast(snap)} == {"passthrough"}
    assert _one(snap, "_calibration")["extra_state_attributes"]["status"] == "no_calibration"


def _spike(expected: bool | None, *, short: bool) -> Callable[[harness.Built, dict], None]:
    def check(built: harness.Built, snap: dict) -> None:
        spikes = [e for e in _forecast(snap) if e["raw_value"] >= 3.0]
        assert spikes, "no interval at or above the spike threshold"
        assert all((e["horizon_hours"] < 24) is short for e in spikes)
        assert all(e["spike_credible"] is expected for e in spikes), spikes
        if short:
            # The chart reads the gate unsuppressed: the covariates do support it.
            chart = {e["time"]: e for e in _one(snap, "_forecast_chart")["forecast_data"]}
            assert all(chart[e["time"]].get("spike_credible") is True for e in spikes)
    return check


def _negative_midday(built: harness.Built, snap: dict) -> None:
    forecast = _forecast(snap)
    assert any(
        e["calibrated_source"] == "isotonic_below_domain" and e["raw_value"] < 0 for e in forecast
    )
    assert any(e["value"] < 0 for e in forecast)
    assert any(e["raw_value"] == -1.0 for e in forecast)
    premium = _one(snap, "_scarcity_premium")
    assert premium["extra_state_attributes"]["status"] == "active"
    assert premium["state"] > 0


def _stage2_out_of_domain(built: harness.Built, snap: dict) -> None:
    """Some in-band interval has a stage-2 model and STPASA features, and the
    model's domain gate refuses it, so the published value stayed stage 1."""
    mods = built.mods
    inputs = mods.calibration_inputs
    engine = mods.calibration_engine
    runtime = built.entry.runtime_data
    coordinator, store = runtime.coordinator, runtime.store
    run_at = coordinator.data.prices[built.scenario.region].forecast_generated_at
    run_features = inputs.run_features_for_coordinator(coordinator)
    declined = 0
    for entry in _forecast(snap):
        h = inputs.horizon_hours(run_at, entry["time"])
        features = inputs.stpasa_features_for_interval(coordinator, entry["time"], h, run_at_iso=run_at)
        if features is None or entry["calibrated_source"] != "isotonic":
            continue
        hour = int(entry["time"][11:13])
        ols = store.calibration.ols_models.get(engine._bucket_key(h, hour))
        if ols is None or len(ols.coef) < 2:
            continue
        vec = [
            engine.stage2_iso_feature({"calibrated": entry["calibrated"]}, entry["raw_value"]),
            run_features.run_max_h6_rrp, run_features.run_mean_rrp, run_features.run_spread,
            h / 168.0,
            features.log_surplus, features.log_solar, features.log_demand, features.poe_spread_n,
        ]
        if not ols.serves(vec):
            declined += 1
    assert declined > 0, "no interval was declined by the stage-2 feature-domain gate"


def _dispatch_live(built: harness.Built, snap: dict) -> None:
    assert _one(snap, "_forecast")["state"] == NSW_DISPATCH_RRP
    assert _one(snap, "_forecast")["state"] != _forecast(snap)[0]["value"]


def _stale(built: harness.Built, snap: dict) -> None:
    attrs = _one(snap, "_forecast")["extra_state_attributes"]
    assert attrs["is_stale"] is True
    assert attrs["stale_reason"]
    assert attrs["data_age_hours"] > 11


def _prcer(built: harness.Built, snap: dict) -> None:
    extension = [
        r for r in snap["entities"].values()
        if (r.get("extra_state_attributes") or {}).get("tariff_source") == "nem_pd7day extension"
    ]
    codes = {(r["extra_state_attributes"]["tariff_code"], "import_tariff_code" in r["extra_state_attributes"]) for r in extension}
    assert ("PRCER", False) in codes and ("PRCER", True) in codes, codes


def _lor2(built: harness.Built, snap: dict) -> None:
    stress = _one(snap, "_grid_stress")
    assert stress["state"] is True
    assert stress["extra_state_attributes"]["stress_level"] == 2
    notices = _one(snap, "_grid_notices")
    assert notices["state"] == 2
    listed = notices["extra_state_attributes"]["notices"]
    assert {n["region"] for n in listed} == {"QLD1"}
    assert {n["notice_id"] for n in listed} == {150211, 150218}


NON_VACUITY: dict[str, Callable[[harness.Built, dict], None]] = {
    "qld_fitted_evening_peak": _fitted_evening_peak,
    "qld_days27_mode": _days27,
    "qld_empty_store": _empty_store,
    "qld_spike_credible": _spike(True, short=False),
    "qld_spike_uncredible": _spike(False, short=False),
    "qld_short_lead_spike": _spike(None, short=True),
    "qld_negative_midday": _negative_midday,
    "qld_stage2_out_of_domain": _stage2_out_of_domain,
    "nsw_dispatch_live": _dispatch_live,
    "sa_stale_coordinator": _stale,
    "vic_prcer_extension": _prcer,
    "qld_lor2_notice": _lor2,
}


# ── The golden master ────────────────────────────────────────────────────────

def _failure(name: str, lines: list[str]) -> str:
    shown = lines[:MAX_DIFF_LINES]
    more = len(lines) - len(shown)
    header = (
        f"golden master {name}: {len(lines)} difference(s) from "
        f"tests/golden/snapshots/{name}.json.gz\n"
        "  unique id | attribute path | expected | actual\n  "
    )
    tail = f"\n  ... and {more} more" if more else ""
    return header + "\n  ".join(shown) + tail


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_golden_master(name: str) -> None:
    with harness.build_entities(name) as built:
        actual = snapshot.snapshot(built.entities, built.scenario, built.domains)
        NON_VACUITY[name](built, actual)
    text = snapshot.dumps(actual)
    path = SNAPSHOT_DIR / f"{name}.json.gz"
    if UPDATE:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        # Gzipped because the tariff sensors publish whole forecasts and the
        # plain JSON is 7.8 MB; mtime=0 keeps the file bytes a pure function
        # of the content, so regenerating an unchanged snapshot changes nothing.
        path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    assert path.exists(), f"no snapshot for {name}; run with GOLDEN_UPDATE=1"
    assert matplotlib.__version__ == snapshot.PNG_MATPLOTLIB_VERSION, (
        f"the forecast chart PNG hashes were recorded on matplotlib "
        f"{snapshot.PNG_MATPLOTLIB_VERSION}, this is {matplotlib.__version__}"
    )
    expected = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    lines = snapshot.diff(expected, json.loads(text))
    if lines:
        pytest.fail(_failure(name, lines), pytrace=False)


def test_every_scenario_has_a_non_vacuity_check() -> None:
    assert set(NON_VACUITY) == set(SCENARIOS)
    assert len(SCENARIOS) == 12


# ── The frozen clock ─────────────────────────────────────────────────────────

def test_clock_scan_finds_no_read_it_cannot_freeze() -> None:
    assert not clock.SCAN.problems, "\n".join(clock.SCAN.problems)
    kinds = clock.SCAN.counts_by_kind()
    assert kinds.get("now_nem", 0) > 0 and kinds.get("dt_util.utcnow", 0) > 0


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    from datetime import datetime\n    return datetime.now()\n",
        "import time\nSTARTED = time.monotonic()\n",
        "import time\nclass C:\n    clock = time.monotonic\n",
        "def f(x):\n    return x.monotonic()\n\nfrom datetime import date as d\ndef g():\n    return d.fromordinal(1).today()\n",
    ],
)
def test_clock_scan_rejects_reads_it_cannot_freeze(source: str) -> None:
    """The scan must fail loudly on a read no binding patch can reach."""
    reads, problems = clock.scan_source("sample", source)
    assert problems, reads


def test_clock_scan_classifies_the_reads_it_freezes() -> None:
    source = (
        "import time\nimport datetime\nfrom datetime import datetime as dt\n"
        "from homeassistant.util import dt as dt_util\nfrom .nem_time import now_nem\n"
        "def f(clock=time.monotonic):\n"
        "    return (time.time(), datetime.datetime.now(), dt.now(), dt_util.utcnow(), now_nem())\n"
    )
    reads, problems = clock.scan_source("sample", source)
    assert not problems
    assert sorted(r.kind for r in reads) == sorted([
        "time.monotonic (default argument)", "time.time", "datetime.datetime.now",
        "datetime.now", "dt_util.utcnow", "now_nem",
    ])
