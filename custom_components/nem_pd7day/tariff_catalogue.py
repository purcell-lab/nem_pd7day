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

from .const import DISTRIBUTOR_TARIFFS, EXPORT_TARIFF_NAMES, TARIFF_NAMES

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


def import_tariffs(distributor: str) -> dict[str, Any]:
    """The library's import tariff table for ``distributor``; {} without the library."""
    return _table(_module(distributor), "get_tariffs", "tariffs")


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


def export_program_supported(distributor: str, export_code: str) -> bool:
    """Whether the library can convert ``export_code`` as a feed-in tariff.

    True without the library (the constants are trusted) and on a network
    that publishes no feed-in table; False only when the network publishes
    one and the code is not in it.
    """
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
    tables = [import_tariffs(distributor), feed_in_tariffs(distributor) or {}]
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
    return next((name for name in fallbacks if name), code)
