"""
Tariff sensors are enumerated from aemo_to_tariff's own catalogue.

Issue #159: every tariff the integration knew about came from hard-coded
lists in const.py, and the friendly-name lookup read a ``module.tariffs``
attribute the library had replaced with ``get_tariffs()``. Against
aemo-to-tariff 0.7.27 that left 25 library tariffs with no sensor, 10 listed
codes the library could not convert (six dead Ergon codes and five feed-in
codes listed as imports) and 41 names silently falling back to stale
constants.

tariff_catalogue reads the library at runtime; const.py is the fallback for a
missing library and the source of what the library cannot say (default
enabled set, import-to-export pairings).

Run with:  python -m pytest tests/test_tariff_catalogue.py -v
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# const first, so the catalogue's relative import resolves without pulling in
# the HA-dependent package __init__.
_const = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_cat = _load(
    "custom_components.nem_pd7day.tariff_catalogue",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "tariff_catalogue.py"),
)

aemo_to_tariff = pytest.importorskip("aemo_to_tariff")

DISTRIBUTORS = sorted({d for ds in _const.REGION_DISTRIBUTORS.values() for d in ds})


def _lib_module(distributor):
    return importlib.import_module("aemo_to_tariff." + {"sapn": "sapower"}.get(distributor, distributor))


def _lib_import_table(distributor):
    mod = _lib_module(distributor)
    with contextlib.redirect_stdout(io.StringIO()):
        getter = getattr(mod, "get_tariffs", None)
        return getter() if callable(getter) else mod.tariffs


def _lib_feed_in_table(distributor):
    mod = _lib_module(distributor)
    getter = getattr(mod, "get_feed_in_tariffs", None)
    if not callable(getter):
        return None
    with contextlib.redirect_stdout(io.StringIO()):
        return getter()


def test_library_is_read():
    assert _cat.library_available()


def test_library_version_is_the_installed_metadata(monkeypatch):
    """The version is published so an install can be checked against the manifest floor."""
    from importlib import metadata

    assert _cat.library_version() == metadata.version("aemo-to-tariff")
    monkeypatch.setattr(_cat, "_att", None)
    assert _cat.library_version() is None


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_import_codes_are_the_library_catalogue(distributor):
    """Every import tariff the library carries gets a sensor, and nothing else."""
    assert sorted(_cat.import_tariff_codes(distributor)) == sorted(_lib_import_table(distributor).keys())


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_import_codes_keep_the_snapshot_order_then_append(distributor):
    """The first default-enabled code decides the day 2-7 default; library order must not move it."""
    codes = _cat.import_tariff_codes(distributor)
    snapshot = [c for c in _const.DISTRIBUTOR_TARIFFS[distributor] if c in codes]
    assert codes[: len(snapshot)] == snapshot
    assert codes[len(snapshot):] == [c for c in _lib_import_table(distributor) if c not in snapshot]


def test_library_order_cannot_change_the_default_tariff(monkeypatch):
    """SAPN listed RTOU before RESELE; the default stays the snapshot's first code."""
    fake = SimpleNamespace(sapower=SimpleNamespace(
        __name__="sapower",
        tariffs={"RTOU": {"name": "a"}, "RESELE": {"name": "b"}, "NEWCODE": {"name": "c"}},
    ))
    monkeypatch.setattr(_cat, "_att", fake)
    codes = _cat.import_tariff_codes("sapn")
    assert codes[0] == _const.DISTRIBUTOR_TARIFFS["sapn"][0] == "RESELE"
    assert codes[-1] == "NEWCODE"


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_feed_in_only_codes_are_not_import_sensors(distributor):
    """N61, BLNREX2 and friends were listed as import tariffs; they are feed-in tariffs."""
    feed_in = _lib_feed_in_table(distributor)
    if feed_in is None:
        pytest.skip("network publishes no feed-in table")
    imports = set(_cat.import_tariff_codes(distributor))
    feed_in_only = set(feed_in) - set(_lib_import_table(distributor))
    assert not (feed_in_only & imports), feed_in_only & imports


def test_default_enabled_tariffs_exist_in_the_catalogue():
    """A default-enabled pair the library cannot convert would be an enabled dead sensor."""
    missing = [
        (d, c) for d, c in _const.DEFAULT_ENABLED_TARIFFS
        if c not in _cat.import_tariff_codes(d)
    ]
    assert not missing, missing


def test_export_programs_are_feed_in_tariffs():
    for distributor in DISTRIBUTORS:
        for import_code, export_code in _cat.export_programs(distributor).items():
            assert import_code in _cat.import_tariff_codes(distributor), (distributor, import_code)
            assert _cat.export_program_supported(distributor, export_code), (distributor, export_code)
            table = _lib_feed_in_table(distributor)
            if table is not None:
                assert export_code in table


# What the three derivation rules yield against 0.7.27. A new pairing in the
# library shows up here as a failure, which is the point: it should also be
# copied into the EXPORT_TARIFF_PROGRAMS fallback.
EXPECTED_EXPORT_PROGRAMS = {
    "energex": {"6900": "6900X", "6800": "6800X", "96200": "96200X"},   # rule 2: code + "X"
    "ergon": {},                                                        # two export codes, one import: unpaired
    "ausgrid": {"EA025": "EA029", "EA225": "EA029"},                    # rule 3: battery_tariffs positional
    "endeavour": {"N71": "N61", "N95": "N95"},                          # rule 3, then rule 1
    "essential": {"BLNRSS2": "BLNREX2", "BLNBSS1": "BLNBEX1"},          # rule 3, library pairing
    "evoenergy": {"026": "026"},                                        # rule 1: same code both ways
    "sapn": {"RESELE": "RESELE", "RELE2W": "RELE2W", "SBELE": "SBELE", "B2R": "B2R"},
}


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_export_programs_are_derived_from_the_library(distributor):
    assert _cat.export_programs(distributor) == EXPECTED_EXPORT_PROGRAMS.get(distributor, {})


def test_export_programs_follow_the_import_order():
    for distributor in DISTRIBUTORS:
        order = _cat.import_tariff_codes(distributor)
        codes = list(_cat.export_programs(distributor))
        assert codes == sorted(codes, key=order.index), distributor


def test_export_codes_the_rules_cannot_place_are_reported():
    # Ergon lists NVGC2 and NVGX2 against each import tariff with nothing
    # saying which applies, so neither is paired and both are reported.
    assert _cat.unpaired_export_codes("ergon") == ["NVGC2", "NVGX2"]
    # SAPN's RESELEX/SBELEX lose to the same-code rule for RESELE/SBELE.
    assert _cat.unpaired_export_codes("sapn") == ["RESELEX", "SBELEX"]
    for distributor in ("energex", "ausgrid", "endeavour", "essential", "evoenergy"):
        assert _cat.unpaired_export_codes(distributor) == [], distributor


def test_export_override_wins_over_the_derived_pairing(monkeypatch):
    monkeypatch.setattr(_const, "EXPORT_TARIFF_OVERRIDES", {
        ("ergon", "ERTOUET1"): "NVGC2",   # a pairing the library cannot express
        ("sapn", "RESELE"): "RESELEX",    # overriding a derived one
    })
    monkeypatch.setattr(_cat, "EXPORT_TARIFF_OVERRIDES", _const.EXPORT_TARIFF_OVERRIDES)
    assert _cat.export_programs("ergon") == {"ERTOUET1": "NVGC2"}
    assert _cat.export_programs("sapn")["RESELE"] == "RESELEX"
    # The displaced RESELE feed-in code is now the one without a sensor.
    assert _cat.unpaired_export_codes("sapn") == ["RESELE", "SBELEX"]


def test_fallback_export_programs_match_the_derivation():
    """EXPORT_TARIFF_PROGRAMS is the no-library fallback; keep it equal to what the library yields."""
    derived = {
        (distributor, import_code): export_code
        for distributor in DISTRIBUTORS
        for import_code, export_code in _cat.export_programs(distributor).items()
    }
    assert _const.EXPORT_TARIFF_PROGRAMS == derived
    for export_code in set(derived.values()):
        assert export_code in _const.EXPORT_TARIFF_NAMES, export_code


def test_export_suffix_names_resolve_to_the_import_tariff():
    """Energex 6900X has no table entry of its own; it is 6900's export side."""
    assert _cat.tariff_name("energex", "6900X", export=True) == _cat.tariff_name("energex", "6900")
    assert _cat.tariff_name("energex", "96200X", export=True) == _cat.tariff_name("energex", "96200")


def test_names_come_from_the_library_not_the_constants():
    """Energex 3900 was 'Residential Transitional Demand' in const.py; the library disagrees."""
    lib_name = _lib_import_table("energex")["3900"]["name"]
    assert _cat.tariff_name("energex", "3900") == lib_name
    assert lib_name != _const.TARIFF_NAMES["energex"]["3900"]
    # Feed-in names resolve through the feed-in table.
    feed_in = _lib_feed_in_table("endeavour")
    assert _cat.tariff_name("endeavour", "N61", export=True) == feed_in["N61"]["name"]
    # An unknown code falls back to itself.
    assert _cat.tariff_name("energex", "ZZZZZ") == "ZZZZZ"


def test_fallback_snapshot_matches_the_pinned_library():
    """DISTRIBUTOR_TARIFFS is the no-library fallback; keep it equal to the pinned catalogue."""
    for distributor in DISTRIBUTORS:
        assert sorted(_const.DISTRIBUTOR_TARIFFS[distributor]) == sorted(_lib_import_table(distributor)), distributor


def test_without_the_library_the_constants_are_used(monkeypatch):
    monkeypatch.setattr(_cat, "_att", None)
    assert not _cat.library_available()
    for distributor in DISTRIBUTORS:
        assert _cat.import_tariff_codes(distributor) == list(_const.DISTRIBUTOR_TARIFFS[distributor])
    assert _cat.export_program_supported("endeavour", "N61")
    for distributor in DISTRIBUTORS:
        assert _cat.export_programs(distributor) == {
            code: export for (d, code), export in _const.EXPORT_TARIFF_PROGRAMS.items() if d == distributor
        }
    assert _cat.tariff_name("energex", "3900") == _const.TARIFF_NAMES["energex"]["3900"]
    assert _cat.tariff_name("endeavour", "N61", export=True) == _const.EXPORT_TARIFF_NAMES["N61"]


def test_a_broken_or_missing_module_yields_no_sensors(monkeypatch):
    """A table that raises, or a network the library lacks, means no catalogue, not a crash."""
    def boom():
        raise RuntimeError("table unavailable")
    fake = SimpleNamespace(
        energex=SimpleNamespace(__name__="energex", get_tariffs=boom),
        sapower=SimpleNamespace(__name__="sapower", tariffs={"RTOU": {"name": "Residential Time of Use"}}),
    )
    monkeypatch.setattr(_cat, "_att", fake)
    assert _cat.import_tariff_codes("energex") == []
    assert _cat.import_tariff_codes("ausgrid") == []
    # The old module-level ``tariffs`` shape still resolves.
    assert _cat.import_tariff_codes("sapn") == ["RTOU"]
    assert _cat.tariff_name("sapn", "RTOU") == "Residential Time of Use"
    # No feed-in table on the fake network: export programs are not refused.
    assert _cat.export_program_supported("sapn", "RESELE")
