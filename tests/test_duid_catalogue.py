"""
The bundled DUID catalogue (duid_catalogue.py and duid_catalogue.json) and
what the local price sensors take from it: the DUID's region, station, fuel
and coordinates, and each region's most constrained DUIDs as disabled presets.

The JSON is built by scripts/build_duid_catalogue.py from AEMO's registration
data, Open Electricity's facility files and a fortnight of DispatchIS
LOCAL_PRICE rows; these tests check its shape, not its contents of the day.

Run with:  python -m pytest tests/test_duid_catalogue.py -v
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const, _cat, _lp, _cc, _coord, _sensor = load_chain(
    "const", "duid_catalogue", "local_price", "constraint_client", "local_price_coordinator", "local_price_sensor"
)

REGIONS = ("QLD1", "NSW1", "VIC1", "SA1", "TAS1")
DISPATCH = (Path(__file__).parent / "fixtures" / "local_price" / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
ENTRY = SimpleNamespace(entry_id="e1")


# ── The bundled file ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bundled():
    _cat.load_catalogue.cache_clear()
    return _cat.load_catalogue()


def test_the_bundled_catalogue_covers_every_region(bundled):
    assert len(bundled.duids) > 500
    assert {i.region for i in bundled.duids.values()} == set(REGIONS)
    assert all(_lp.DUID_RE.match(d) for d in bundled.duids), "every registered DUID passes the options check"


def test_each_region_has_up_to_five_presets_of_its_own(bundled):
    for region in REGIONS:
        top = bundled.top_for(region)
        assert 0 < len(top) <= 5 and len(set(top)) == len(top)
        assert all(bundled.info(d).region == region for d in top)


def test_coordinates_are_in_australia_or_absent(bundled):
    located = [i for i in bundled.duids.values() if i.latitude is not None]
    assert len(located) > 400
    assert all(-44 < i.latitude < -10 and 112 < i.longitude < 154 for i in located)
    assert all(i.longitude is None for i in bundled.duids.values() if i.latitude is None)


def test_known_duids(bundled):
    assert bundled.info("GSTONE1").region == "QLD1"
    assert bundled.info("CATHROCK").region == "SA1"
    assert bundled.info("BARRON-1").region == "QLD1"  # a hyphen, as AEMO registers it


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_malformed_rows_are_skipped_not_raised():
    catalogue = _cat.parse_catalogue({
        "duids": {
            "OK1": {"region": "QLD1", "station": "Ok", "lat": -27.5, "lon": 153.0, "constrained_intervals": 4},
            "NOREGION": {"station": "x"},
            "BADROW": "nope",
            "NOCOORD": {"region": "QLD1", "lat": "?", "lon": True},
        },
        "top": {"QLD1": ["OK1", "MISSING"], "NSW1": "nope"},
    })
    assert set(catalogue.duids) == {"OK1", "NOCOORD"}
    assert catalogue.info("NOCOORD").latitude is None and catalogue.info("NOCOORD").longitude is None
    assert catalogue.info("NOCOORD").station == "NOCOORD"
    assert catalogue.top_for("QLD1") == ("OK1",) and catalogue.top_for("NSW1") == ()
    assert _cat.parse_catalogue(None) is _cat.EMPTY


def test_a_missing_or_corrupt_file_is_an_empty_catalogue(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{not json")
    assert _cat.load_catalogue.__wrapped__(bad) is _cat.EMPTY
    assert _cat.load_catalogue.__wrapped__(tmp_path / "missing.json") is _cat.EMPTY


def test_region_order_is_most_constrained_first_and_labels_name_the_station():
    catalogue = _cat.parse_catalogue({"duids": {
        "B1": {"region": "QLD1", "station": "Bravo", "fuel": "solar_utility", "constrained_intervals": 1},
        "A1": {"region": "QLD1", "station": "Alpha", "constrained_intervals": 1},
        "C1": {"region": "QLD1", "station": "Charlie", "constrained_intervals": 9},
        "N1": {"region": "NSW1", "station": "Nsw"},
    }})
    assert [i.duid for i in catalogue.region_duids("QLD1")] == ["C1", "A1", "B1"]
    assert catalogue.info("B1").label == "B1 · Bravo (solar utility)"
    assert catalogue.info("A1").label == "A1 · Alpha"


# ── Sensors ──────────────────────────────────────────────────────────────────

CATALOGUE = _cat.parse_catalogue({
    "duids": {
        "CATHROCK": {"region": "SA1", "station": "Cathedral Rocks", "fuel": "wind", "lat": -34.84874, "lon": 135.59265},
        "KEPSF1": {"region": "QLD1", "station": "Kennedy Energy Park", "lat": -20.87279, "lon": 144.41303},
        "KEPWF1": {"region": "QLD1", "station": "Kennedy Energy Park"},
        "GSTONE1": {"region": "QLD1", "station": "Gladstone"},
    },
    "top": {"QLD1": ["KEPSF1", "KEPWF1", "GSTONE1"]},
})


def _constraints():
    prices = _lp.parse_dispatch_local_prices(DISPATCH, "f.zip", datetime(2026, 10, 30, 21, 25, 40, tzinfo=NEM_TZ))
    return SimpleNamespace(tracker=SimpleNamespace(latest=SimpleNamespace(local_prices=prices)))


def test_presets_are_disabled_and_a_chosen_preset_is_not_repeated():
    entities = _sensor.local_price_entities(
        _constraints(), SimpleNamespace(latest=None), ENTRY, "QLD1", ["GSTONE1", "CATHROCK"], False, CATALOGUE
    )
    by_id = {e._attr_unique_id: e._attr_entity_registry_enabled_default for e in entities}
    assert by_id["e1_QLD1_GSTONE1_local_price_adjustment"] is True
    assert "e1_QLD1_GSTONE1_local_price" not in by_id  # chosen, and the diagnostic is off
    assert by_id["e1_QLD1_KEPSF1_local_price"] is False  # a preset carries all three
    assert sum(1 for k in by_id if "_GSTONE1_" in k) == 2
    assert [k for k, on in by_id.items() if not on] == [
        f"e1_QLD1_{d}_{s}" for d in ("KEPSF1", "KEPWF1")
        for s in ("local_price_adjustment", "local_price_adjustment_forecast", "local_price")
    ]


def test_a_catalogued_duid_publishes_its_station_and_coordinates(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ))
    sensor = _sensor.NemPd7dayLocalPriceAdjustmentSensor(
        _constraints(), ENTRY, "QLD1", "CATHROCK", CATALOGUE.info("CATHROCK")
    )
    attrs = sensor.extra_state_attributes
    assert (attrs["latitude"], attrs["longitude"]) == (-34.84874, 135.59265)
    assert (attrs["station"], attrs["fuel"], attrs["duid_region"]) == ("Cathedral Rocks", "wind", "SA1")
    plain = _sensor.NemPd7dayLocalPriceAdjustmentSensor(_constraints(), ENTRY, "QLD1", "UNKNOWN1")
    assert "latitude" not in plain.extra_state_attributes


def test_the_diagnostic_uses_the_duid_regions_rop():
    """Live on 31 Oct a QLD1 entry priced CATHROCK (SA) on QLD1's ROP. With the
    catalogue it takes SA1's: 28.67941 - 33.92 = -5.24 $/MWh."""
    with patch.object(_sensor, "now_nem", lambda: datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)):
        sa = _sensor.NemPd7dayLocalPriceSensor(_constraints(), ENTRY, "QLD1", "CATHROCK", CATALOGUE.info("CATHROCK"))
        unknown = _sensor.NemPd7dayLocalPriceSensor(_constraints(), ENTRY, "QLD1", "CATHROCK")
        assert sa.native_value == pytest.approx(-0.005241)  # rounded to 6 places
        assert sa.extra_state_attributes["rop_region"] == "SA1"
        assert unknown.extra_state_attributes["rop_region"] == "QLD1"  # not in the catalogue: the entry's
        assert unknown.native_value == pytest.approx((85.25 - 33.92) / 1000)


# ── Enabling a chosen preset ─────────────────────────────────────────────────

class _Registry:
    def __init__(self, rows):
        self.rows = rows

    def async_get_entity_id(self, domain, platform, unique_id):
        return next((r.entity_id for r in self.rows.values() if r.unique_id == unique_id), None)

    def async_get(self, entity_id):
        return self.rows.get(entity_id)

    def async_update_entity(self, entity_id, **changes):
        for key, value in changes.items():
            setattr(self.rows[entity_id], key, value)


def test_choosing_a_preset_enables_what_the_integration_disabled():
    integration = _sensor.er.RegistryEntryDisabler.INTEGRATION
    user = _sensor.er.RegistryEntryDisabler.USER
    rows = {
        "sensor.a": SimpleNamespace(entity_id="sensor.a", unique_id="e1_QLD1_KEPSF1_local_price_adjustment", disabled_by=integration),
        "sensor.b": SimpleNamespace(entity_id="sensor.b", unique_id="e1_QLD1_KEPSF1_local_price", disabled_by=user),
    }
    with patch.object(_sensor.er, "async_get", return_value=_Registry(rows)):
        assert _sensor.enable_chosen(None, ENTRY, "QLD1", ["KEPSF1"]) == 1
    assert rows["sensor.a"].disabled_by is None
    assert rows["sensor.b"].disabled_by == user  # the user's choice stands


# ── P5MIN only while a forecast sensor listens ───────────────────────────────

def test_p5min_is_not_fetched_until_a_forecast_sensor_listens():
    coord = _coord.P5MinCoordinator(SimpleNamespace(), None, SimpleNamespace())
    requests = []
    coord._guard = SimpleNamespace(request=lambda: requests.append(1))
    coord._listeners = {}
    coord._on_dispatch()
    assert requests == []
    coord._listeners = {object(): None}
    coord._on_dispatch()
    coord.request_refresh()
    assert requests == [1, 1]


def test_an_added_forecast_sensor_asks_for_the_first_run():
    asked = []
    p5 = SimpleNamespace(latest=None, request_refresh=lambda: asked.append(1))
    sensor = _sensor.NemPd7dayLocalPriceAdjustmentForecastSensor(p5, ENTRY, "QLD1", "KEPSF1")

    async def _noop(self):
        return None

    with patch.object(_sensor.CoordinatorEntity, "async_added_to_hass", _noop, create=True):
        run_async(sensor.async_added_to_hass())
    assert asked == [1]


def test_the_json_and_the_builder_agree_on_shape():
    data = json.loads(_cat.PATH.read_text())
    assert {"generated", "constrained_window", "top", "duids"} <= set(data)
    row = next(iter(data["duids"].values()))
    assert {"region", "station", "type", "fuel", "lat", "lon", "constrained_intervals"} <= set(row)
