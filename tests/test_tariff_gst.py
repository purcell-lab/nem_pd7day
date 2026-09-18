"""
GST is applied exactly once to every component of an import tariff, issue #158.

``aemo_to_tariff`` composes a recognised tariff as spot plus network rate, in
c/kWh. Seven of the thirteen distributor modules apply GST to the network rate
themselves and six do not, so the library's output is a mixture: the spot
component is always GST exclusive, and the network component is GST inclusive
for some networks and not others.

``tariff_sensor.py`` used to multiply the whole library result by 1.1, which
charged GST twice on the network component for the seven, by the network rate
times 0.1. On the live install that overstated the Energex 6900 evening peak by
0.021486 $/kWh, 6.24 percent; SAPN is worse at about 0.0396 $/kWh. GST applies
to general tariffs and not to feed-in tariffs, so the export path adds none and
is unaffected, which is asserted here rather than assumed.

The tests are in two groups.

The ``test_catalogue_*`` and ``test_library_*`` probes read every import tariff
the library publishes, through ``tariff_catalogue`` so they follow the same
enumeration the sensors do (#159), and compare the network component the
library returns against the rates in the library's own table. They fail if a
network changes its GST treatment, if a new network appears unclassified, or if
the probe itself stops resolving. They are behavioural on purpose: scanning the
library source for ``GST`` misclassifies evoenergy, which uses a lower case
local named ``gst``, and that mistake was actually made while developing this
fix.

The rest assert the integration's arithmetic. The ``affected`` cases fail
against the pre-fix formula; the ``unaffected`` cases pass either way and guard
against a blanket removal of the GST multiply, which would break the six
correct networks in the opposite direction.

Run with:  python -m pytest tests/test_tariff_gst.py -v
"""
from __future__ import annotations

import contextlib
import datetime
import io
from unittest.mock import MagicMock

import pytest

from support import install_ha_stubs, load_chain

install_ha_stubs()

_const_mod, _nem_time, _client_mod, _store_mod, _coord_mod, tariff_catalogue, _tariff_mod = load_chain(
    "const", "nem_time", "pd7day_client", "calibration_store", "coordinator",
    "tariff_catalogue", "tariff_sensor",
)

GST = _tariff_mod.GST
_DEFAULT_DLF = _tariff_mod._DEFAULT_DLF
_DEFAULT_MARKET = _tariff_mod._DEFAULT_MARKET
_DEFAULT_MLF = _tariff_mod._DEFAULT_MLF
_LIB_APPLIES_GST = _tariff_mod._LIB_APPLIES_GST
NemPd7dayTariffSensor = _tariff_mod.NemPd7dayTariffSensor

pytest.importorskip("aemo_to_tariff")
from aemo_to_tariff import spot_to_tariff  # noqa: E402

pytestmark = pytest.mark.skipif(
    not tariff_catalogue.library_available(),
    reason="aemo_to_tariff not installed",
)

NEM = datetime.timezone(datetime.timedelta(hours=10))

# A fixed evening interval END. Time of use tariffs are in a peak period here,
# where the network rate, and therefore the double counted GST, is largest.
PEAK = datetime.datetime(2026, 9, 18, 18, 30, tzinfo=NEM)

DISTRIBUTORS = sorted(_const_mod.DISTRIBUTOR_TARIFFS)

REGIONS = {
    "energex": "QLD1", "ergon": "QLD1",
    "ausgrid": "NSW1", "endeavour": "NSW1", "essential": "NSW1", "evoenergy": "NSW1",
    "sapn": "SA1",
    "powercor": "VIC1", "united": "VIC1", "jemena": "VIC1",
    "victoria": "VIC1", "ausnet": "VIC1",
    "tasnetworks": "TAS1",
}


def _spot_component(rrp_mwh: float) -> float:
    """The library's spot component in c/kWh, by its own formula."""
    return rrp_mwh * _DEFAULT_DLF * _DEFAULT_MLF * _DEFAULT_MARKET / 10


def _convert(distributor: str, code: str, rrp_mwh: float) -> float:
    """spot_to_tariff with the loss factors pinned, stdout suppressed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return spot_to_tariff(
            PEAK, distributor, code, rrp_mwh,
            dlf=_DEFAULT_DLF, mlf=_DEFAULT_MLF, market=_DEFAULT_MARKET,
        )


def _network_component(distributor: str, code: str) -> float:
    """The library's network component in c/kWh, isolated at zero spot."""
    return _convert(distributor, code, 0.0)


def _table_rates(entry: dict) -> list[float]:
    """Every network rate in a library tariff entry.

    Period tuples are not one shape across the library: most networks put the
    rate at index 3, SAPN carries an extra field and puts it at index 4. Every
    numeric from index 3 on is collected rather than indexing a fixed position,
    so a network that grows another field does not silently stop being probed.
    """
    rates: list[float] = []
    for period in entry.get("periods", []):
        rates.extend(v for v in period[3:] if isinstance(v, (int, float)))
    rate = entry.get("rate")
    if isinstance(rate, (int, float)):
        rates.append(rate)
    elif isinstance(rate, dict):
        rates.extend(v for v in rate.values() if isinstance(v, (int, float)))
    return rates


def _observed_gst_factor(distributor: str, code: str, entry: dict) -> float | None:
    """1.1 if the library GSTs this network rate, 1.0 if not, None if unresolved.

    The returned network component is matched against every rate in the
    library's own table, so no period selection is needed and the result does
    not depend on which period the fixed timestamp happens to land in.

    None means the component matched no published rate, which is how the
    library's ``spot * slope + intercept`` fallback for a code its convert path
    does not recognise shows up. That fallback is not a spot plus network
    composition at all, so it has no GST factor to read.
    """
    try:
        observed = _network_component(distributor, code)
    except Exception:
        return None
    for rate in _table_rates(entry):
        if rate and abs(observed - rate) < 1e-4:
            return 1.0
        if rate and abs(observed - rate * GST) < 1e-4:
            return GST
    return None


def _catalogue(distributor: str) -> dict[str, dict]:
    """Import tariffs for a distributor, entries only."""
    return {
        code: entry
        for code, entry in tariff_catalogue.import_tariffs(distributor).items()
        if isinstance(entry, dict)
    }


def _probe(distributor: str) -> tuple[set[float], list[str]]:
    """(factors observed, codes that did not resolve) across the whole catalogue."""
    factors: set[float] = set()
    unresolved: list[str] = []
    for code, entry in _catalogue(distributor).items():
        factor = _observed_gst_factor(distributor, code, entry)
        if factor is None:
            unresolved.append(code)
        else:
            factors.add(factor)
    return factors, unresolved


def _representative(distributor: str) -> str:
    """A catalogue code whose GST factor resolves, for the arithmetic tests."""
    for code, entry in _catalogue(distributor).items():
        if _observed_gst_factor(distributor, code, entry) is not None:
            return code
    pytest.skip(f"no resolvable tariff in the catalogue for {distributor}")


# ── Library drift probes, over the whole catalogue ───────────────────────────


def test_probe_resolves_almost_every_catalogue_tariff():
    """The probe must keep its grip on the library's tables.

    Without this, a library restructure that made the tables unreadable would
    turn the parametrised probes below into a row of vacuous passes and the
    drift detector would quietly stop detecting anything.
    """
    total = sum(len(_catalogue(d)) for d in DISTRIBUTORS)
    unresolved = [f"{d}/{c}" for d in DISTRIBUTORS for c in _probe(d)[1]]
    assert total >= 100, f"only {total} import tariffs enumerated; catalogue looks broken"
    assert len(unresolved) <= total * 0.05, (
        f"{len(unresolved)} of {total} catalogue tariffs could not be matched against "
        f"the library's own rates: {unresolved[:10]}. The GST probe has lost its grip "
        f"on the library's tables and can no longer detect a change in treatment."
    )


def test_every_catalogue_distributor_is_classified():
    """A network the library gains must be classified, not silently defaulted.

    _LIB_APPLIES_GST is a membership test, so an unknown network falls to the
    "library applies no GST" branch. That is the right default for the six, and
    wrong by 10 percent of the network rate for anything like the seven.
    """
    unclassified = [
        d for d in DISTRIBUTORS
        if _catalogue(d) and _probe(d)[0] == {GST} and d not in _LIB_APPLIES_GST
    ]
    assert not unclassified, (
        f"{unclassified} apply GST to their network rate but are absent from "
        f"_LIB_APPLIES_GST, so their prices carry GST twice."
    )


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_catalogue_gst_factor_is_uniform_per_distributor(distributor):
    """Every tariff on a network must share one GST treatment.

    _LIB_APPLIES_GST is keyed by distributor, not by tariff, so a network that
    GSTs some of its tariffs and not others could not be corrected by it. This
    fails loudly if the library ever becomes mixed within a network.
    """
    factors, _ = _probe(distributor)
    if not factors:
        pytest.skip(f"no resolvable tariffs for {distributor}")
    assert len(factors) == 1, (
        f"{distributor} applies mixed GST treatment across its tariffs: {factors}. "
        f"_LIB_APPLIES_GST cannot express this and the correction must move to "
        f"per-tariff."
    )


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_library_gst_grouping_matches_constant(distributor):
    """_LIB_APPLIES_GST must match what the installed library actually does."""
    factors, _ = _probe(distributor)
    if not factors:
        pytest.skip(f"no resolvable tariffs for {distributor}")
    observed = factors.pop()
    expected = GST if distributor in _LIB_APPLIES_GST else 1.0
    assert observed == expected, (
        f"{distributor} applies a network GST factor of {observed} but "
        f"_LIB_APPLIES_GST implies {expected}. aemo_to_tariff has changed its GST "
        f"treatment for this network; update _LIB_APPLIES_GST in tariff_sensor.py "
        f"or every price on it will be wrong by 10 percent of the network rate."
    )


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_spot_component_is_never_gst_inclusive(distributor):
    """The library never GSTs the spot component, on any network.

    The fix grosses the spot component up unconditionally, so this has to hold
    everywhere, not only on the networks in _LIB_APPLIES_GST.
    """
    code = _representative(distributor)
    rrp_mwh = 100.0
    observed_spot = _convert(distributor, code, rrp_mwh) - _network_component(distributor, code)
    expected = _spot_component(rrp_mwh)
    assert abs(observed_spot - expected) < 1e-4, (
        f"{distributor}/{code}: spot component {observed_spot} is not the library's "
        f"own formula {expected}"
    )
    assert abs(observed_spot - expected * GST) > 1e-4, (
        f"{distributor}/{code} returns a GST inclusive spot component; the single "
        f"gross up in _retail_price would then double count it."
    )


# ── Integration arithmetic ───────────────────────────────────────────────────


def _price(distributor: str, code: str, calibrated: float, fee: float = 0.0) -> float:
    """Published import price in $/kWh for one interval, through a real sensor."""
    coordinator = MagicMock()
    coordinator.data = None
    entry = MagicMock()
    entry.entry_id = "entry_gst"
    entry.options = {}
    sensor = NemPd7dayTariffSensor(coordinator, entry, REGIONS[distributor], distributor, code)
    rrp_mwh = calibrated * 1000
    return sensor._retail_price(_convert(distributor, code, rrp_mwh), rrp_mwh, fee)


def _old_price(distributor: str, code: str, calibrated: float, fee: float = 0.0) -> float:
    """The pre-fix formula: one gross up over the library's combined output."""
    rrp_mwh = calibrated * 1000
    return round((_convert(distributor, code, rrp_mwh) / 100 + fee) * GST, 6)


AFFECTED = sorted(_LIB_APPLIES_GST)
UNAFFECTED = [d for d in DISTRIBUTORS if d not in _LIB_APPLIES_GST]


@pytest.mark.parametrize("distributor", AFFECTED)
def test_affected_network_carries_single_gst(distributor):
    """Fails against main, where the network component carried GST twice."""
    code = _representative(distributor)
    calibrated = 0.10
    got = _price(distributor, code, calibrated)

    network_c = _network_component(distributor, code) / GST
    expected = round(((_spot_component(calibrated * 1000) + network_c) / 100) * GST, 6)
    assert got == pytest.approx(expected, abs=1e-6)

    # old - new = ((spot + rate*GST) - (spot + rate)) / 100 * GST
    #           = rate * 0.1 * GST / 100, and rate * GST is the network component,
    # so the overstatement is a tenth of the GST inclusive network rate.
    old = _old_price(distributor, code, calibrated)
    overstatement = round(_network_component(distributor, code) * (GST - 1.0) / 100, 6)
    assert old - got == pytest.approx(overstatement, abs=1e-6), (
        f"{distributor}/{code}: expected the old formula to overstate by a tenth of "
        f"the GST inclusive network rate, {overstatement} $/kWh, got {old - got}"
    )
    assert old > got, "the fix must lower the price on an affected network"


@pytest.mark.parametrize("distributor", UNAFFECTED)
def test_unaffected_network_price_is_unchanged(distributor):
    """Guards against a blanket removal of the GST multiply.

    These networks were already correct, so the new code must agree with the
    old formula exactly.
    """
    code = _representative(distributor)
    calibrated = 0.10
    got = _price(distributor, code, calibrated)
    old = _old_price(distributor, code, calibrated)
    assert got == pytest.approx(old, abs=1e-6), (
        f"{distributor}/{code} was already correct and must not move: "
        f"old {old}, new {got}"
    )


def test_energex_6900_evening_peak_matches_hand_computation():
    """A worked example pinned to published numbers, not to the library's tables.

    Energex 6900 Evening is a published 19.533 c/kWh network rate. At a
    calibrated spot of 0.10 $/kWh the correct price grosses up the sum of the
    spot and network components exactly once.
    """
    if "6900" not in _catalogue("energex"):
        pytest.skip("energex 6900 not in this library's catalogue")
    expected = round(((_spot_component(100.0) + 19.533) / 100) * GST, 6)
    got = _price("energex", "6900", 0.10)
    assert got == pytest.approx(expected, abs=1e-6), (
        f"expected {expected} $/kWh from spot {_spot_component(100.0):.4f} c/kWh plus "
        f"network 19.533 c/kWh grossed up once, got {got}"
    )
    # The live reading this bug was found on, and its correction.
    assert _old_price("energex", "6900", 0.10) - got == pytest.approx(0.021486, abs=1e-6)


def test_zero_spot_leaves_only_the_gst_inclusive_network_rate():
    """At zero spot the price is the network rate with exactly one GST."""
    if "6900" not in _catalogue("energex"):
        pytest.skip("energex 6900 not in this library's catalogue")
    assert _price("energex", "6900", 0.0) == pytest.approx(round(19.533 * GST / 100, 6), abs=1e-6)


def test_additional_usage_fee_is_grossed_up_once():
    """The usage fee is added before GST and must still be grossed up."""
    code = _representative("energex")
    without = _price("energex", code, 0.10, fee=0.0)
    with_fee = _price("energex", code, 0.10, fee=0.05)
    assert with_fee - without == pytest.approx(0.05 * GST, abs=1e-6)


def test_feed_in_path_has_no_gst_anywhere():
    """GST applies to general tariffs and not to feed-in tariffs.

    The export sensor adds no GST, which is only correct if the library does
    not add one inside its feed-in conversion either. If a network ever starts
    grossing up its feed-in rate, export prices become GST inclusive and this
    fails.
    """
    import aemo_to_tariff as att

    grossed_up = []
    for distributor in DISTRIBUTORS:
        table = tariff_catalogue.feed_in_tariffs(distributor)
        if not table:
            continue
        module = getattr(att, tariff_catalogue._LIB_MODULE.get(distributor, distributor), None)
        convert = getattr(module, "convert_feed_in_tariff", None)
        if convert is None:
            continue
        for code, entry in table.items():
            if not isinstance(entry, dict):
                continue
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    observed = convert(PEAK, code, 0.0)
            except Exception:
                continue
            for rate in _table_rates(entry):
                if rate and abs(observed - rate * GST) < 1e-4:
                    grossed_up.append(f"{distributor}/{code}")
                    break
    assert not grossed_up, (
        f"{grossed_up} return a GST inclusive feed-in rate. Feed-in tariffs do not "
        f"attract GST and the export sensor adds none, so these exports are now "
        f"overstated by 10 percent of the feed-in rate."
    )
