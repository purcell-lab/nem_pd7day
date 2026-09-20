"""Which tariffs exist, read from aemo_to_tariff at runtime.

The integration used to enumerate tariff sensors from hard-coded lists in
const.py, so a tariff added to the library did nothing until someone edited
the integration, and the friendly-name lookup read a ``module.tariffs``
attribute the library had replaced with per-financial-year tables behind
``get_tariffs()``. Measured against aemo-to-tariff 0.7.27 that left 25
library tariffs with no sensor, 10 listed codes the library could not
convert, and 41 names silently falling back to stale constants. Issue #159.

This module is the single reader of the library's catalogue. The constants
in const.py remain as the fallback for when the library is not importable,
and for what the library cannot say: which tariffs are enabled by default,
and which import tariff pairs with which export program.

No Home Assistant imports: the config flow and the sensor platform both call
this, and tests exercise it without the HA stubs.
"""

from __future__ import annotations

import contextlib
import io
import logging
from importlib import metadata
from typing import Any

from .const import (
    DISTRIBUTOR_TARIFFS,
    EXPORT_TARIFF_NAMES,
    EXPORT_TARIFF_OVERRIDES,
    EXPORT_TARIFF_PROGRAMS,
    TARIFF_NAMES,
)
from . import tariff_extensions

_LOGGER = logging.getLogger(__name__)

try:
    import aemo_to_tariff as _att
except ImportError:  # pragma: no cover - exercised by tests through monkeypatching
    _att = None  # type: ignore[assignment]

# const.py distributor keys that differ from the library's module names.
_LIB_MODULE = {
    "sapn": "sapower",
}


def library_available() -> bool:
    """True when aemo_to_tariff imported."""
    return _att is not None


def _read_library_version() -> str | None:
    if _att is None:
        return None
    try:
        return metadata.version("aemo-to-tariff")
    except metadata.PackageNotFoundError:
        return None


# Read once at import. importlib.metadata reads the package's dist-info from
# disk, and diagnostics and state writes run on the event loop, where Home
# Assistant flags a file read from a custom integration; the import itself
# already happens off the loop.
_LIBRARY_VERSION = _read_library_version()


def library_version() -> str | None:
    """Installed aemo-to-tariff version, or None when it is not importable.

    Published on the tariff sensors and in diagnostics so an install can be
    checked against the floor in manifest.json without shell access: the
    catalogue follows whatever version is installed, and nothing else on the
    system said which that was (issue #159).
    """
    return _LIBRARY_VERSION if _att is not None else None


def _module(distributor: str) -> Any | None:
    if _att is None:
        return None
    return getattr(_att, _LIB_MODULE.get(distributor, distributor), None)


def _table(module: Any, getter: str, attribute: str) -> dict[str, Any]:
    """A tariff table from ``module.<getter>()`` or, failing that, ``module.<attribute>``.

    The library moved from a module-level dict to a per-year getter; both
    shapes are read so an older or newer library still resolves. stdout is
    suppressed because sapower.py prints while building its tables.
    """
    if module is None:
        return {}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn = getattr(module, getter, None)
            table = fn() if callable(fn) else getattr(module, attribute, None)
    except Exception:  # noqa: BLE001 - a broken table is "no catalogue", not a crash
        _LOGGER.debug("aemo_to_tariff %s.%s failed", module.__name__, getter, exc_info=True)
        return {}
    return table if isinstance(table, dict) else {}


def _library_import_tariffs(distributor: str) -> dict[str, Any]:
    return _table(_module(distributor), "get_tariffs", "tariffs")


def import_tariffs(distributor: str) -> dict[str, Any]:
    """The library's import tariff table for ``distributor``, plus any extension
    tariff it lacks (tariff_extensions, issue #170); {} without the library."""
    table = dict(_library_import_tariffs(distributor))
    for code, entry in tariff_extensions.import_table(distributor).items():
        if code not in table:
            table[code] = entry
    return table


_logged_superseded: set[tuple[str, str]] = set()


def priced_by_extension(distributor: str, code: str, *, export: bool = False) -> bool:
    """Whether ``code`` is priced from tariff_extensions rather than the library.

    True only while the library lacks the code: the library wins as soon as
    an installed release carries it, and the first time that is seen the
    superseded entry is logged so it can be deleted.
    """
    ext = tariff_extensions.get(distributor, code)
    if ext is None or (export and not ext.feed_in_periods):
        return False
    library = (feed_in_tariffs(distributor) or {}) if export else _library_import_tariffs(distributor)
    if code in library:
        if (distributor, code) not in _logged_superseded:
            _logged_superseded.add((distributor, code))
            _LOGGER.info(
                "aemo-to-tariff %s now carries %s/%s; the tariff_extensions entry is ignored and can be removed",
                library_version(), distributor, code,
            )
        return False
    return True


def feed_in_tariffs(distributor: str) -> dict[str, Any] | None:
    """The library's feed-in table, or None when the module has none.

    None rather than {} because several networks (Ausgrid among them) convert
    feed-in tariffs without publishing a table, and an export program on such
    a network must not be refused for the table's absence.
    """
    module = _module(distributor)
    if module is None or not (hasattr(module, "get_feed_in_tariffs") or hasattr(module, "feed_in_tariffs")):
        return None
    return _table(module, "get_feed_in_tariffs", "feed_in_tariffs")


def import_tariff_codes(distributor: str) -> list[str]:
    """Import tariff codes to build sensors for.

    From the library when it is importable, from DISTRIBUTOR_TARIFFS
    otherwise. A code the library does not carry is not returned even if the
    constant lists it: the library could not convert it, so the sensor would
    never publish a price.

    Order is DISTRIBUTOR_TARIFFS' order for the codes both have, then the
    library's new codes in the library's order. The order is load-bearing:
    the day 2 to 7 tariff sensor and the config flow default to the FIRST
    default-enabled code for the region when no active tariff is set, and
    on the first deploy of the library-driven catalogue SA Power Networks'
    table happened to list RTOU before RESELE, which silently moved that
    default from RESELE to RTOU and retired the entity a live install was
    using. Keeping the snapshot's order pins the default; a new library
    code can only ever be appended.
    """
    if _att is None:
        return list(DISTRIBUTOR_TARIFFS.get(distributor, []))
    library = list(import_tariffs(distributor).keys())
    known = [code for code in DISTRIBUTOR_TARIFFS.get(distributor, []) if code in library]
    return known + [code for code in library if code not in known]


def _battery_lists(distributor: str) -> list[tuple[list[str], list[str]]]:
    """(import codes, export codes) per customer type from battery_tariffs().

    The library's own statement of which tariffs pair for a battery customer.
    Not every module has it, and the two lists are not always the same
    length, so the pairing has to be inferred; see export_programs.
    """
    module = _module(distributor)
    fn = getattr(module, "battery_tariffs", None) if module is not None else None
    if not callable(fn):
        return []
    out: list[tuple[list[str], list[str]]] = []
    for customer_type in ("Residential", "Business"):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pairs = fn(customer_type)
        except Exception:  # noqa: BLE001 - an unknown customer type is "no data"
            continue
        if isinstance(pairs, dict):
            imports = [str(c) for c in pairs.get("import") or []]
            exports = [str(c) for c in pairs.get("export") or []]
            out.append((imports, exports))
    return out


def _library_export_codes(distributor: str) -> set[str]:
    """Every code the library, or an extension, treats as an export tariff for ``distributor``."""
    codes: set[str] = set(feed_in_tariffs(distributor) or {})
    for _imports, exports in _battery_lists(distributor):
        codes.update(exports)
    codes.update(tariff_extensions.feed_in_codes(distributor))
    return codes


def export_programs(distributor: str) -> dict[str, str]:
    """Import code → export code for the export sensors of ``distributor``.

    Derived from the library where it is importable (issue #159). Three rules,
    in order, each applied only to import codes still unpaired:

    1. The same code is both an import tariff and an export tariff (SAPN
       RESELE, RELE2W, SBELE, B2R; Endeavour N95; Evoenergy 026).
    2. The import code with an ``X`` suffix is an export tariff (Energex 6900X,
       96200X, 6800X).
    3. ``battery_tariffs()`` lists the same number of imports and exports for a
       customer type, taken positionally (Ausgrid EA025→EA029 and EA225→EA029,
       Endeavour N71→N61, Essential BLNRSS2→BLNREX2 and BLNBSS1→BLNBEX1).

    What the library cannot express is left to EXPORT_TARIFF_OVERRIDES, which
    also wins over a derived pairing for the same import code. Ergon is the
    live case: one import tariff against two export codes, NVGC2 and NVGX2,
    with nothing saying which is which; see unpaired_export_codes. Without
    the library the EXPORT_TARIFF_PROGRAMS snapshot is used.

    Order follows import_tariff_codes so the entity list is stable.
    """
    if _att is None:
        pairs = {code: export for (d, code), export in EXPORT_TARIFF_PROGRAMS.items() if d == distributor}
    else:
        imports = import_tariff_codes(distributor)
        exports = _library_export_codes(distributor)
        pairs = {}
        for code in imports:
            if code in exports:
                pairs[code] = code
        for code in imports:
            if code not in pairs and f"{code}X" in exports:
                pairs[code] = f"{code}X"
        for battery_imports, battery_exports in _battery_lists(distributor):
            if len(battery_imports) != len(battery_exports):
                continue
            for code, export in zip(battery_imports, battery_exports):
                if code in imports and code not in pairs and export in exports:
                    pairs[code] = export
        pairs = {code: pairs[code] for code in imports if code in pairs}
    for (d, code), export in EXPORT_TARIFF_OVERRIDES.items():
        if d == distributor:
            pairs[code] = export
    return pairs


def unpaired_export_codes(distributor: str) -> list[str]:
    """Export codes the library carries that no rule could attach to an import."""
    paired = set(export_programs(distributor).values())
    return sorted(code for code in _library_export_codes(distributor) if code not in paired)


def export_program_supported(distributor: str, export_code: str) -> bool:
    """Whether the library can convert ``export_code`` as a feed-in tariff.

    True without the library (the constants are trusted) and on a network
    that publishes no feed-in table; False only when the network publishes
    one and the code is not in it.
    """
    if priced_by_extension(distributor, export_code, export=True):
        return True
    table = feed_in_tariffs(distributor)
    if table is None:
        return True
    return export_code in table


def tariff_name(distributor: str, code: str, *, export: bool = False) -> str:
    """Friendly name from the library's tables, then the constants, then the code.

    ``export`` only orders the lookups: a feed-in code is looked up in the
    feed-in table and EXPORT_TARIFF_NAMES before the import equivalents, so
    a code that appears in both (SAPN RESELE) names the right program.
    """
    feed_in = dict(feed_in_tariffs(distributor) or {})
    for ext_code, ext_entry in tariff_extensions.feed_in_table(distributor).items():
        feed_in.setdefault(ext_code, ext_entry)
    tables = [import_tariffs(distributor), feed_in]
    fallbacks = [TARIFF_NAMES.get(distributor, {}).get(code), EXPORT_TARIFF_NAMES.get(code)]
    if export:
        tables.reverse()
        fallbacks.reverse()
    for table in tables:
        entry = table.get(code)
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                return str(name)
    if export and code.endswith("X"):
        # Energex names its export programs after the import tariff plus X and
        # publishes no feed-in table, so the import tariff's name is the name.
        entry = import_tariffs(distributor).get(code[:-1])
        if isinstance(entry, dict) and entry.get("name"):
            return str(entry["name"])
    return next((name for name in fallbacks if name), code)
