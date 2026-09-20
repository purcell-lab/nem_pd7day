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
    from custom_components.nem_pd7day import tariff_extensions
    expected = set(_lib_import_table(distributor)) | set(tariff_extensions.import_codes(distributor))
    assert sorted(_cat.import_tariff_codes(distributor)) == sorted(expected)


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_import_codes_keep_the_snapshot_order_then_append(distributor):
    """The first default-enabled code decides the day 2-7 default; library order must not move it."""
    codes = _cat.import_tariff_codes(distributor)
    snapshot = [c for c in _const.DISTRIBUTOR_TARIFFS[distributor] if c in codes]
    assert codes[: len(snapshot)] == snapshot
    from custom_components.nem_pd7day import tariff_extensions
    library_new = [c for c in _lib_import_table(distributor) if c not in snapshot]
    extension = [c for c in tariff_extensions.import_codes(distributor) if c not in library_new]
    assert codes[len(snapshot):] == library_new + extension


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


# What the derivation yields against 0.7.27, per distributor: the import→export
# pairings and the export codes no rule could place. A library release that adds
# a pairing fails here, which is the point: EXPORT_TARIFF_PROGRAMS (the
# no-library fallback) must be refreshed to match.
EXPECTED_EXPORT_PROGRAMS = {
    "energex": ({"6900": "6900X", "6800": "6800X", "96200": "96200X"}, []),      # code + "X"
    "ergon": ({}, ["NVGC2", "NVGX2"]),                                          # two exports, one import
    "ausgrid": ({"EA025": "EA029", "EA225": "EA029"}, []),                       # battery_tariffs, positional
    "endeavour": ({"N71": "N61", "N95": "N95"}, []),                             # positional, then same code
    "essential": ({"BLNRSS2": "BLNREX2", "BLNBSS1": "BLNBEX1"}, []),             # positional
    "evoenergy": ({"026": "026"}, []),                                           # same code both ways
    "sapn": ({"RESELE": "RESELE", "RELE2W": "RELE2W", "SBELE": "SBELE", "B2R": "B2R"},
             ["RESELEX", "SBELEX"]),                                             # same code beats the X twin
    "powercor": ({"PRCER": "PRCER"}, []),                                        # tariff_extensions (#170)
}


@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_export_programs_are_derived_from_the_library(distributor):
    expected, unpaired = EXPECTED_EXPORT_PROGRAMS.get(distributor, ({}, []))
    programs = _cat.export_programs(distributor)
    assert programs == expected
    assert _cat.unpaired_export_codes(distributor) == unpaired
    # Every pairing is an import sensor's code against a convertible feed-in code.
    imports = _cat.import_tariff_codes(distributor)
    table = _lib_feed_in_table(distributor)
    for import_code, export_code in programs.items():
        assert import_code in imports
        assert _cat.export_program_supported(distributor, export_code)
        assert table is None or export_code in table
    # Entity order follows the import catalogue.
    assert list(programs) == [c for c in imports if c in programs]
    # The no-library fallback and its names are kept in step.
    assert {c: e for (d, c), e in _const.EXPORT_TARIFF_PROGRAMS.items() if d == distributor} == programs
    assert all(e in _const.EXPORT_TARIFF_NAMES for e in programs.values())


def test_export_override_wins_over_the_derived_pairing(monkeypatch):
    monkeypatch.setattr(_cat, "EXPORT_TARIFF_OVERRIDES", {
        ("ergon", "ERTOUET1"): "NVGC2",   # a pairing the library cannot express
        ("sapn", "RESELE"): "RESELEX",    # overriding a derived one
    })
    assert _cat.export_programs("ergon") == {"ERTOUET1": "NVGC2"}
    assert _cat.export_programs("sapn")["RESELE"] == "RESELEX"
    # The displaced RESELE feed-in code is now the one without a sensor.
    assert _cat.unpaired_export_codes("sapn") == ["RESELE", "SBELEX"]


def test_names_come_from_the_library_not_the_constants():
    """Energex 3900 was 'Residential Transitional Demand' in const.py; the library disagrees."""
    lib_name = _lib_import_table("energex")["3900"]["name"]
    assert _cat.tariff_name("energex", "3900") == lib_name
    assert lib_name != _const.TARIFF_NAMES["energex"]["3900"]
    # Feed-in names resolve through the feed-in table.
    feed_in = _lib_feed_in_table("endeavour")
    assert _cat.tariff_name("endeavour", "N61", export=True) == feed_in["N61"]["name"]
    # Energex 6900X has no table entry of its own; it is 6900's export side.
    assert _cat.tariff_name("energex", "6900X", export=True) == _cat.tariff_name("energex", "6900")
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
