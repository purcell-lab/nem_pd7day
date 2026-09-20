"""Short-lead suppression of the published spike_credible attribute.

Calibration against 817 PD7DAY archive runs (2024-06 to 2026-09, 105,620
forecast intervals at or above $3000/MWh) showed the covariate gate selects a
worse subset than the raw forecast alone inside 24 hours: it flagged 272
intervals at 15.81 percent precision against a 20.42 percent base rate, and in
QLD1 it flagged 3 and got none of them right. The published attribute therefore
reports None inside that horizon.

The chart callout path is deliberately left on the raw gate result, so these
tests also pin that the store itself is unchanged.
See docs/spike_threshold_calibration.md.
"""
import pytest

from custom_components.nem_pd7day.const import SPIKE_COVARIATE_MIN_HORIZON_H
from custom_components.nem_pd7day.sensor import _published_spike_credible


def test_horizon_constant_is_24h():
    assert SPIKE_COVARIATE_MIN_HORIZON_H == 24.0


@pytest.mark.parametrize("credible", [True, False])
@pytest.mark.parametrize("horizon", [0.5, 6.0, 12.0, 18.0, 23.5])
def test_gate_is_not_published_inside_the_horizon(credible, horizon):
    """Inside 24h neither a pass nor a fail is published."""
    out = _published_spike_credible({"spike_credible": credible}, horizon)
    assert out is None, (
        f"horizon {horizon}h with gate {credible} should publish None, got {out}"
    )


def test_boundary_is_exclusive():
    """Exactly at the horizon the gate is published again."""
    cal = {"spike_credible": True}
    assert _published_spike_credible(cal, SPIKE_COVARIATE_MIN_HORIZON_H) is True


@pytest.mark.parametrize("credible", [True, False])
@pytest.mark.parametrize("horizon", [24.0, 30.0, 48.0, 96.0, 167.5])
def test_gate_is_published_unchanged_beyond_the_horizon(credible, horizon):
    out = _published_spike_credible({"spike_credible": credible}, horizon)
    assert out is credible, (
        f"horizon {horizon}h should publish {credible}, got {out}"
    )


def test_absent_key_stays_none():
    """Below spike territory the gate never ran and None is passed through."""
    assert _published_spike_credible({}, 96.0) is None
    assert _published_spike_credible({}, 1.0) is None


def test_explicit_none_stays_none_beyond_the_horizon():
    """A covariate that could not be answered is still None, not False."""
    assert _published_spike_credible({"spike_credible": None}, 96.0) is None


def test_missing_horizon_does_not_suppress():
    """A missing horizon must not be guessed into short lead."""
    assert _published_spike_credible({"spike_credible": True}, None) is True
    assert _published_spike_credible({"spike_credible": False}, None) is False


def test_store_gate_itself_is_unsuppressed():
    """
    The store must keep returning the raw gate at short lead, because the
    camera callout path reads it directly and was deliberately left gated.
    """
    from tests.test_calibration_store import _make_store_with_calibration
    store = _make_store_with_calibration()
    result = store.apply_to_price(
        5.0, 6.0, 14, gas_forecast_tj=200.0, network_tight=True,
    )
    assert result.get("spike_credible") is True, (
        "the store gate must stay raw at short lead so callouts are unchanged"
    )
