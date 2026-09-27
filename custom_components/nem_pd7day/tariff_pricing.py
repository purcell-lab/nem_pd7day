"""Tariff pricing: a spot price in, a network tariff price out.

The rules for turning a spot price into a network tariff price used to live
inside the tariff sensors, mixed in with Home Assistant entity code. The PRCER
extension (#172) had to route every priced call through module-level wrappers
in tariff_sensor because pricing could not be injected. Spec 001 moves those
rules here, and they have one reason to change: the tariff rules themselves.
That covers which source prices a code (the ``aemo_to_tariff`` library or the
tariff_extensions table, #170), how the library's inconsistent GST treatment
is corrected (#158), how period rows are normalised, and how time-of-use
windows are parsed and matched (#62).

The entities keep everything else: caching, exception policy and logging,
clock reads, and attribute building. Nothing here catches an exception from a
pricing call, and nothing here reads the clock; callers pass the time in.

Domain layer: no Home Assistant imports and no nem_time import, so the whole
module runs under tests without the HA stubs.
"""
from __future__ import annotations

import contextlib
import datetime  # module import: datetime.datetime is looked up at call time
import io
import logging
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from . import tariff_catalogue, tariff_extensions

_LOGGER = logging.getLogger(__name__)

# Bound once here and looked up as module globals at call time, so a test that
# patches one of these names on this module reaches every pricer. The names
# are None when the library is not importable; see library_available.
try:
    from aemo_to_tariff import get_daily_fee, get_periods, spot_to_feed_in_tariff, spot_to_tariff
except ImportError:
    spot_to_tariff = None  # type: ignore[assignment]
    spot_to_feed_in_tariff = None  # type: ignore[assignment]
    get_periods = None  # type: ignore[assignment]
    get_daily_fee = None  # type: ignore[assignment]
    _LOGGER.warning("aemo_to_tariff not installed — tariff sensors will be unavailable")

# Default loss factors used by aemo_to_tariff library (Energex defaults).
# These are passed to the library explicitly rather than relying on its own
# defaults, because RetailPrice.import_dollars below reconstructs the spot
# component from them and the two sides have to agree. If the library ever
# changes its defaults, an implicit match would break the split silently.
DEFAULT_DLF: Final[float] = 1.05905
DEFAULT_MLF: Final[float] = 1.0154
DEFAULT_MARKET: Final[float] = 1.0154

# Published as the combined_loss_multiplier attribute of every tariff sensor.
COMBINED_LOSS_MULTIPLIER: Final[float] = round(DEFAULT_DLF * DEFAULT_MLF * DEFAULT_MARKET, 6)

# GST multiplier applied to the final retail price.
GST: Final[float] = 1.1

# Distributor modules inside aemo_to_tariff that apply GST to the network rate
# themselves, returning "spot (GST exclusive) + network rate * 1.1". The other
# supported modules apply no GST at all and return both components GST
# exclusive. The library is simply not consistent about this, so we cannot
# apply one uniform multiply to its output without double counting GST on the
# network component for the distributors listed here. See issue #158.
#
# Keys are const.py distributor keys, which are what gets passed to the
# library; it recognises "sapn" directly and routes it to its sapower module.
#
# This restates a fact about a pinned third party library, so
# tests/test_tariff_gst.py probes the installed library at test time and fails
# if a distributor moves between the two groups rather than trusting this set.
# The probe is behavioural, comparing the library's network component against
# the rates in its own tariff table, because a source scan for "GST" gets this
# wrong: evoenergy applies GST through a lower case local named "gst" and would
# be misclassified as not applying it.
LIB_APPLIES_GST: Final[frozenset[str]] = frozenset(
    {"energex", "ergon", "ausgrid", "endeavour", "essential", "evoenergy", "sapn"}
)

SOURCE_LIBRARY: Final[str] = "aemo-to-tariff"
SOURCE_EXTENSION: Final[str] = "nem_pd7day extension"

# A library period row: (name, start, end, rate_c) on most networks, and
# (name, start, end, condition, rate_c) on SAPN.
PeriodRow = tuple[Any, ...]
# An extension feed-in row: (name, start, end, adjustment c/kWh).
FeedInRow = tuple[str, datetime.time, datetime.time, float]


def library_available() -> bool:
    """True when all four aemo_to_tariff functions are bound.

    The tariff sensors guard every priced call on this, extension codes
    included: without the library the sensors are unavailable as a whole.
    """
    return (
        spot_to_tariff is not None
        and spot_to_feed_in_tariff is not None
        and get_periods is not None
        and get_daily_fee is not None
    )


@contextlib.contextmanager
def quiet_stdout() -> Iterator[None]:
    """Suppress stdout to silence debug print() calls in aemo_to_tariff library."""
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = old_stdout


class TariffPricer(Protocol):
    """Prices one tariff code of one distributor, in the library's units (c/kWh).

    A pricer raises whatever its underlying call raises; the exception policy
    stays with the caller.
    """

    @property
    def distributor(self) -> str: ...

    @property
    def code(self) -> str: ...

    @property
    def source(self) -> str: ...

    def import_c_kwh(self, interval_end: datetime.datetime, rrp_mwh: float) -> float: ...

    def feed_in_c_kwh(self, interval_end: datetime.datetime, rrp_mwh: float) -> float: ...

    def period_rows(self, now: datetime.datetime) -> list[PeriodRow]: ...

    def feed_in_rows(self, now: datetime.datetime) -> list[FeedInRow]: ...

    def daily_fee(self) -> float | None: ...


@dataclass(frozen=True)
class LibraryPricer:
    """A tariff aemo_to_tariff carries. Every library call runs inside quiet_stdout().

    aemo_to_tariff/sapower.py contains debug print() calls, which would
    otherwise reach the Home Assistant log.
    """

    distributor: str
    code: str
    source: str = SOURCE_LIBRARY

    def import_c_kwh(self, interval_end: datetime.datetime, rrp_mwh: float) -> float:
        """The library's spot_to_tariff, with the loss factors passed explicitly.

        The library expects AEMO nemtime (interval END) and subtracts 5 min
        internally for period lookup. The loss factors are passed so that
        RetailPrice can reconstruct the spot component the library used.
        """
        with quiet_stdout():
            return spot_to_tariff(
                interval_end, self.distributor, self.code, rrp_mwh,
                dlf=DEFAULT_DLF, mlf=DEFAULT_MLF, market=DEFAULT_MARKET,
            )

    def feed_in_c_kwh(self, interval_end: datetime.datetime, rrp_mwh: float) -> float:
        """The library's spot_to_feed_in_tariff, with its own default loss factors."""
        with quiet_stdout():
            return spot_to_feed_in_tariff(interval_end, self.distributor, self.code, rrp_mwh)

    def period_rows(self, now: datetime.datetime) -> list[PeriodRow]:
        """The library's period rows, consumed inside the suppression; ``now`` is unused."""
        with quiet_stdout():
            return list(get_periods(self.distributor, self.code))

    def feed_in_rows(self, now: datetime.datetime) -> list[FeedInRow]:
        """Always empty: the library exposes no feed-in period or rate data."""
        return []

    def daily_fee(self) -> float | None:
        with quiet_stdout():
            return get_daily_fee(self.distributor, self.code)


@dataclass(frozen=True)
class ExtensionPricer:
    """A tariff the installed library lacks, priced from tariff_extensions (#170)."""

    distributor: str
    code: str
    source: str = SOURCE_EXTENSION

    def import_c_kwh(self, interval_end: datetime.datetime, rrp_mwh: float) -> float:
        return tariff_extensions.spot_to_tariff(
            interval_end, self.distributor, self.code, rrp_mwh,
            dlf=DEFAULT_DLF, mlf=DEFAULT_MLF, market=DEFAULT_MARKET,
        )

    def feed_in_c_kwh(self, interval_end: datetime.datetime, rrp_mwh: float) -> float:
        # The library's spot_to_feed_in_tariff applies its default loss
        # factors to the spot component; an extension export must price the
        # same way or it sits 9 per cent off the library's for the same spot.
        return tariff_extensions.spot_to_feed_in_tariff(
            interval_end, self.distributor, self.code, rrp_mwh,
            dlf=DEFAULT_DLF, mlf=DEFAULT_MLF, market=DEFAULT_MARKET,
        )

    def period_rows(self, now: datetime.datetime) -> list[PeriodRow]:
        """The import rows in force at ``now``: an extension tariff's windows follow the season."""
        return list(tariff_extensions.get_periods(self.distributor, self.code, now))

    def feed_in_rows(self, now: datetime.datetime) -> list[FeedInRow]:
        """The export credit or charge rows in force for the month of ``now``."""
        return tariff_extensions.feed_in_periods_for(self.distributor, self.code, now)

    def daily_fee(self) -> float | None:
        return tariff_extensions.get_daily_fee(self.distributor, self.code)


def pricer_for(distributor: str, code: str, *, export: bool = False) -> TariffPricer:
    """The extension pricer exactly when the catalogue routes the code to it, else the library's."""
    if tariff_catalogue.priced_by_extension(distributor, code, export=export):
        return ExtensionPricer(distributor, code)
    return LibraryPricer(distributor, code)


@dataclass(frozen=True)
class RetailPrice:
    """The published price in $/kWh from a pricer's c/kWh result."""

    distributor: str

    def import_dollars(self, library_c_kwh: float, rrp_mwh: float, fee: float) -> float:
        """Combine a library result with the usage fee and GST, in $/kWh.

        ``aemo_to_tariff`` composes a recognised tariff as spot plus network
        rate, but only some of its distributor modules apply GST to the network
        rate. Applying one uniform multiply to the combined figure therefore
        charged GST twice on the network component for those distributors, by
        the network rate times 0.1: about 2.1 c/kWh on Energex 6900 in the
        evening peak, and about 4.0 on SAPN RESELE (#158).

        The two components are separated using the library's own formula. For a
        recognised tariff the network component does not vary with price, so
        the split is exact. The library's GST is then removed from the network
        component only where the library applied it, which leaves a single GST
        multiply here covering spot, network and the usage fee, and keeps the
        published ``gst_multiplier`` attribute an honest description.

        For an unrecognised tariff code the library falls back to
        ``spot * slope + intercept``, which is not a spot plus network
        composition. The split still runs and still yields a single GST on the
        result, but what it treats as the network component is a residual that
        varies with price, so GST placement on that path is approximate. That
        fallback is an acknowledged approximation in the library itself.

        The float operation order is part of the contract: the published value
        must not move by a bit (spec 001, invariant 1).
        """
        spot_c_kwh = rrp_mwh * DEFAULT_DLF * DEFAULT_MLF * DEFAULT_MARKET / 10
        network_c_kwh = library_c_kwh - spot_c_kwh
        if self.distributor in LIB_APPLIES_GST:
            network_c_kwh /= GST
        return round(((spot_c_kwh + network_c_kwh) / 100 + fee) * GST, 6)

    @staticmethod
    def export_dollars(library_c_kwh: float) -> float:
        """A feed-in price in $/kWh: no usage fee and no GST."""
        return round(library_c_kwh / 100, 6)


def period_attributes(rows: Iterable[PeriodRow]) -> list[dict[str, Any]]:
    """Period rows as the ``tariff_periods`` attribute, rates converted to $/kWh."""
    periods = []
    for row in rows:
        # aemo_to_tariff returns 4-tuples for most networks but
        # 5-tuples for SAPN: (name, start, end, condition, rate_c)
        # Use positional unpacking: first 3 fixed, rate_c always last.
        if len(row) < 4:
            continue
        name, start, end = row[0], row[1], row[2]
        rate_c = row[-1]
        if rate_c is None:
            continue
        # Some tariffs (e.g. SAPN SBTOU/SBTOUNE) include an "Off-peak"
        # fallback row with no time window (start/end None). It carries
        # no displayable period, so skip it silently rather than letting
        # start.strftime() raise AttributeError into the caller's handler.
        if start is None or end is None:
            continue
        periods.append({
            "period": name,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "network_rate_$/kwh": round(rate_c / 100, 6),
        })
    return periods


def feed_in_period_attributes(rows: Iterable[FeedInRow]) -> list[dict[str, Any]]:
    """Extension feed-in rows as the ``export_periods`` attribute.

    The credit or charge rows in force this month, rate in $/kWh added to the
    price paid.
    """
    return [
        {
            "period": name,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "export_adjustment_$/kwh": round(rate / 100, 6),
        }
        for name, start, end, rate in rows
    ]


@dataclass(frozen=True)
class TouWindows:
    """Time-of-use windows parsed once from a ``tariff_periods`` list (#62).

    Parsing every entry on every interval cost 21,140 ``strptime`` calls in a
    five build profile, so the tariff sensor parses once per period list and
    keeps the result.
    """

    windows: tuple[tuple[datetime.time, datetime.time, str | None, float | None], ...]

    @classmethod
    def parse(cls, periods: Sequence[Mapping[str, Any]]) -> TouWindows:
        """Parse each entry's "%H:%M" start and end.

        A malformed entry raises: it is deliberately not skipped, so the caller
        yields (None, None) for the interval exactly as the inline parsing did,
        rather than letting a later window match. ``datetime.datetime`` is
        looked up at call time.
        """
        return cls(tuple(
            (
                datetime.datetime.strptime(entry["start"], "%H:%M").time(),
                datetime.datetime.strptime(entry["end"], "%H:%M").time(),
                entry.get("period"),
                entry.get("network_rate_$/kwh"),
            )
            for entry in periods
        ))

    def lookup(self, interval_end: datetime.datetime) -> tuple[str | None, float | None]:
        """The (period name, network $/kWh) for the interval ending at ``interval_end``.

        Mirrors the aemo_to_tariff period lookup: interval END minus 5 min, as
        a time of day, matched first-wins against each [start, end) window,
        with wraparound when start is after end. (None, None) when none match.
        """
        t = (interval_end - datetime.timedelta(minutes=5)).time()
        for start, end, name, rate in self.windows:
            if start <= t < end or (start > end and (t >= start or t < end)):
                return name, rate
        return None, None
