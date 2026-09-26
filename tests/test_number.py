"""
Tests for number.py: AdditionalFeeNumber (RestoreNumber).

The entity starts at DEFAULT_ADDITIONAL_FEE (0.0293 $/kWh) and restores a
previously saved value on startup.

Run with:  python -m pytest tests/test_number.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from support import install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const_mod, _number_mod = load_chain("const", "number")

AdditionalFeeNumber = _number_mod.AdditionalFeeNumber
DEFAULT_ADDITIONAL_FEE = _const_mod.DEFAULT_ADDITIONAL_FEE
DOMAIN = _const_mod.DOMAIN


def make_entity(region="QLD1") -> AdditionalFeeNumber:
    entry = MagicMock()
    entry.entry_id = "entry_1"
    return AdditionalFeeNumber(entry, region)


def test_additional_fee_defaults_and_device_info():
    entity = make_entity("QLD1")

    assert entity._attr_native_value == DEFAULT_ADDITIONAL_FEE
    assert abs(entity._attr_native_value - 0.0293) < 1e-9
    assert entity._attr_unique_id == "nem_pd7day_QLD1_additional_usage_fee"
    assert entity._attr_name == "Additional Usage Fees"
    assert entity._attr_native_unit_of_measurement == "$/kWh"
    assert entity._attr_native_min_value == 0.0
    assert entity._attr_native_max_value == 1.0
    assert entity._attr_native_step == 0.0001
    assert (DOMAIN, "entry_1_QLD1") in entity.device_info["identifiers"]


@pytest.mark.parametrize(
    "last_data, expected",
    [
        pytest.param(None, DEFAULT_ADDITIONAL_FEE, id="no prior data keeps the default"),
        pytest.param(MagicMock(native_value=None), DEFAULT_ADDITIONAL_FEE, id="prior data without a value keeps the default"),
        pytest.param(MagicMock(native_value=0.0500), 0.0500, id="saved value is restored"),
    ],
)
def test_additional_fee_restores_saved_value_on_startup(last_data, expected):
    entity = make_entity("NSW1")
    entity.async_get_last_number_data = AsyncMock(return_value=last_data)

    run_async(entity.async_added_to_hass())

    assert abs(entity._attr_native_value - expected) < 1e-9
