"""
The loss factor option: one combined factor spot is scaled by in every
tariff price, import and export, in place of the library's Energex defaults.

Settled Amber Express prices on the live Energex 96200 site follow
RRP x 1.0272 exactly (576 intervals, 16 to 18 Oct 2026), against the
library's 1.05905 x 1.0154 x 1.0154 = 1.0919. The anchor below is the live
interval 06:50 to 06:55 on 31 Oct 2026: feed-in 0.0661529 and general
0.1677609 $/kWh, spot with GST 0.0708391 $/kWh.

Run with:  python -m pytest tests/test_loss_factor.py -v
"""
from __future__ import annotations

import datetime as dt
import types
from unittest.mock import MagicMock, patch

import pytest

from support import install_ha_stubs, load_chain

install_ha_stubs()

_const, _catalogue, _active, _pricing = load_chain("const", "tariff_catalogue", "active_tariff", "tariff_pricing")

pytest.importorskip("aemo_to_tariff")
from aemo_to_tariff import spot_to_feed_in_tariff, spot_to_tariff  # noqa: E402

NEM = dt.timezone(dt.timedelta(hours=10))
INTERVAL_END = dt.datetime(2026, 10, 31, 6, 55, tzinfo=NEM)
AMBER_FEED_IN = 0.0661529
AMBER_GENERAL = 0.1677609
AMBER_SPOT_GST = 0.0708391
RRP = AMBER_FEED_IN / 1.0272 * 1000     # $/MWh, the dispatch price of that interval
AMBER_FEE = 0.02567                      # Amber's usage fee, $/kWh, as Amber's import shows it


# ── losses_for ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "1.0272", True, 0.5, 1.5, float("nan")])
def test_anything_but_a_number_in_range_keeps_the_library_defaults(value):
    assert _pricing.losses_for(value) is _pricing.DEFAULT_LOSSES


def test_a_combined_factor_is_passed_as_the_dlf():
    losses = _pricing.losses_for(1.0272)
    assert (losses.dlf, losses.mlf, losses.market, losses.source) == (1.0272, 1.0, 1.0, "option")
    assert losses.combined == 1.0272
    assert _pricing.DEFAULT_LOSSES.combined == _pricing.COMBINED_LOSS_MULTIPLIER == 1.09192


# ── Default prices do not move ───────────────────────────────────────────────

def test_default_pricers_price_exactly_as_the_library_defaults():
    pricer = _pricing.pricer_for("energex", "96200")
    assert pricer.import_c_kwh(INTERVAL_END, RRP) == spot_to_tariff(INTERVAL_END, "energex", "96200", RRP)
    # The export used to call the library without loss factors: its defaults are ours.
    assert pricer.feed_in_c_kwh(INTERVAL_END, RRP) == spot_to_feed_in_tariff(INTERVAL_END, "energex", "96200", RRP)


# ── The option prices as Amber does ──────────────────────────────────────────

def test_with_amber_factor_the_feed_in_matches_amber_settled():
    losses = _pricing.losses_for(1.0272)
    c_kwh = _pricing.pricer_for("energex", "96200", export=True, losses=losses).feed_in_c_kwh(INTERVAL_END, RRP)
    assert _pricing.RetailPrice.export_dollars(c_kwh) == pytest.approx(AMBER_FEED_IN, abs=1e-6)
    default = _pricing.pricer_for("energex", "96200", export=True).feed_in_c_kwh(INTERVAL_END, RRP)
    assert _pricing.RetailPrice.export_dollars(default) == pytest.approx(AMBER_FEED_IN * 1.0919 / 1.0272, abs=1e-5)


def test_with_amber_factor_the_import_matches_amber_settled():
    losses = _pricing.losses_for(1.0272)
    c_kwh = _pricing.pricer_for("energex", "96200", losses=losses).import_c_kwh(INTERVAL_END, RRP)
    price = _pricing.RetailPrice("energex", losses).import_dollars(c_kwh, RRP, AMBER_FEE)
    assert price == pytest.approx(AMBER_GENERAL, abs=1e-5)


def test_the_export_charge_window_keeps_its_charge():
    """11:00 to 13:00, April to October: RRP x 1.0272 less 2.21 c, as Amber settles it."""
    noon = dt.datetime(2026, 10, 20, 12, 0, tzinfo=NEM)
    losses = _pricing.losses_for(1.0272)
    c_kwh = _pricing.pricer_for("energex", "96200", export=True, losses=losses).feed_in_c_kwh(noon, 50.0)
    assert c_kwh == pytest.approx(50.0 * 1.0272 / 10 - 2.21, abs=1e-9)


def test_the_gst_split_uses_the_same_factor():
    """RetailPrice takes spot out with the factor the library put in, so the
    network component, and the GST on it, are exact under the option too."""
    losses = _pricing.losses_for(1.0272)
    c_kwh = _pricing.pricer_for("energex", "96200", losses=losses).import_c_kwh(INTERVAL_END, RRP)
    network_ex_gst = (c_kwh - RRP * 1.0272 / 10) / _pricing.GST
    assert network_ex_gst == pytest.approx(6.069, abs=1e-9)  # the 96200 shoulder rate


# ── Attributes ───────────────────────────────────────────────────────────────

def test_loss_attributes_name_where_the_factor_came_from():
    assert _pricing.loss_attributes(_pricing.losses_for(1.0272)) == {
        "distribution_loss_factor_dlf": 1.0272,
        "metering_loss_factor_mlf": 1.0,
        "market_loss_factor": 1.0,
        "combined_loss_multiplier": 1.0272,
        "loss_factor_source": "option",
    }
    assert _pricing.loss_attributes(_pricing.DEFAULT_LOSSES)["loss_factor_source"] == "library_default"


# ── What Amber's settled interval shows ──────────────────────────────────────

class _States:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


def _row(entity_id, platform="amber_express"):
    return types.SimpleNamespace(entity_id=entity_id, platform=platform, domain="sensor")


def _observed(rows, entity_id="sensor.amber_express_amber_feed_in_price", platform="amber_express"):
    hass = types.SimpleNamespace(
        states=_States({entity_id: types.SimpleNamespace(state="0.06", attributes={"detailedForecast": rows})}),
    )
    registry = types.SimpleNamespace(entities={entity_id: _row(entity_id, platform)})
    with patch.object(_active.er, "async_get", return_value=registry):
        return _active.amber_loss_factor(hass)


SETTLED = {"per_kwh": AMBER_FEED_IN, "spot_per_kwh": AMBER_SPOT_GST, "estimate": False}
# A forecast interval follows Amber's prediction, not spot x factor (0.9527 here).
FORECAST = {"per_kwh": 0.0540638, "spot_per_kwh": 0.062425, "estimate": True}


def test_amber_settled_interval_shows_its_factor():
    assert _observed([SETTLED, FORECAST]) == 1.0272


def test_forecast_intervals_and_tiny_spot_show_nothing():
    assert _observed([FORECAST]) is None
    assert _observed([{**SETTLED, "spot_per_kwh": 0.005}]) is None
    assert _observed([{"estimate": False}]) is None
    assert _observed([SETTLED], platform="other") is None
    assert _active.amber_loss_factor(None) is None


def test_the_general_price_is_not_read():
    assert _observed([SETTLED], entity_id="sensor.amber_express_amber_general_price") is None


# ── Tariff sensors read the option ───────────────────────────────────────────

def test_a_tariff_sensor_reads_its_entry_option():
    _tariff = load_chain("nem_time", "pd7day_client", "calibration_store", "coordinator", "tariff_sensor")[-1]
    with_option = types.SimpleNamespace(_entry=types.SimpleNamespace(options={_const.CONF_LOSS_FACTOR: 1.0272}))
    without = types.SimpleNamespace(_entry=types.SimpleNamespace(options={}))
    mock_entry = types.SimpleNamespace(_entry=MagicMock())
    assert _tariff._losses_of(with_option).combined == 1.0272
    assert _tariff._losses_of(without) == _tariff.tariff_pricing.DEFAULT_LOSSES
    assert _tariff._losses_of(mock_entry) == _tariff.tariff_pricing.DEFAULT_LOSSES
