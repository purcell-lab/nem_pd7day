"""Tariffs the installed aemo-to-tariff lacks, priced here until it carries them.

The tariff catalogue is the library's (issue #159), so a tariff the library
does not know has no sensor. A new network tariff reaches the library on the
library's release cadence, which can trail the network's schedule by months;
this table carries such a tariff in the meantime, in the library's own
conventions, so the sensor exists on the day the tariff does (issue #170).

Every entry is temporary by construction: the catalogue lets the library
win, so once an installed release carries the code the entry here is
ignored, logged once, and can be deleted. The entry names the upstream
change it is waiting on.

Conventions, matching ``aemo_to_tariff``:
  * the interval passed in is the interval END (AEMO nemtime); five minutes
    are stepped back before the period lookup, as every library module does;
  * network rates are c/kWh, GST exclusive where the library's module for the
    network is (Powercor's is), so the sensor's single GST multiply applies;
  * feed-in rows carry the c/kWh added to the price the customer is paid,
    credit positive, charge negative.
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)

# (name, start, end, rate c/kWh): the library's 4-tuple period row.
Period = tuple[str, datetime.time, datetime.time, float]
# (name, start, end, months, rate c/kWh): a feed-in row limited to months.
FeedInPeriod = tuple[str, datetime.time, datetime.time, tuple[int, ...], float]


@dataclass(frozen=True)
class ExtensionTariff:
    """One tariff the library lacks: import periods by season, optional export side."""

    distributor: str
    code: str
    name: str
    timezone: str
    daily_fee_c: float
    # Season key -> import period rows. ``season_of`` maps a local datetime to a key.
    seasons: dict[str, list[Period]]
    season_months: dict[str, tuple[int, ...]]
    feed_in_name: str | None = None
    feed_in_periods: list[FeedInPeriod] = field(default_factory=list)
    source: str = ""
    remove_when: str = ""

    def season_of(self, local: datetime.datetime) -> str:
        for key, months in self.season_months.items():
            if local.month in months:
                return key
        raise ValueError(f"{self.distributor}/{self.code}: no season covers month {local.month}")

    def local(self, interval_end: datetime.datetime) -> datetime.datetime:
        """The library's lookup instant: interval end less five minutes, network local time."""
        if interval_end.tzinfo is None:
            interval_end = interval_end.replace(tzinfo=ZoneInfo(self.timezone))
        return (interval_end - datetime.timedelta(minutes=5)).astimezone(ZoneInfo(self.timezone))

    def periods(self, interval_time: datetime.datetime | None = None) -> list[Period]:
        when = interval_time if interval_time is not None else datetime.datetime.now(ZoneInfo(self.timezone))
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo(self.timezone))
        return self.seasons[self.season_of(when.astimezone(ZoneInfo(self.timezone)))]

    def network_rate(self, interval_end: datetime.datetime) -> float | None:
        local = self.local(interval_end)
        t = local.time()
        for _name, start, end, rate in self.seasons[self.season_of(local)]:
            if start <= t < end:
                return rate
        return None

    def feed_in_adjustment(self, interval_end: datetime.datetime) -> float:
        local = self.local(interval_end)
        t = local.time()
        for _name, start, end, months, rate in self.feed_in_periods:
            if local.month in months and start <= t < end:
                return rate
        return 0.0


def _t(h: int, m: int = 0) -> datetime.time:
    return datetime.time(h, m)


_PRCER_PEAK_SEASON = (12, 1, 2, 6, 7, 8)
_PRCER_SHOULDER_SEASON = (3, 4, 5, 9, 10, 11)
_PRCER_SAVER_EXPORT_MONTHS = (9, 10, 11, 12, 1, 2, 3, 4, 5)

EXTENSIONS: dict[tuple[str, str], ExtensionTariff] = {
    # Powercor Residential CER, the opt-in two-way tariff. Powercor 2026-27
    # Tariff Summary (7 May 2026), sheet PAL_2026-27_NUOS, row "Residential
    # CER", GST exclusive. The 1 kWh/day free export allowance on the saver
    # export charge is a daily quantity and is not modelled per interval.
    ("powercor", "PRCER"): ExtensionTariff(
        distributor="powercor",
        code="PRCER",
        name="Residential CER",
        timezone="Australia/Melbourne",
        daily_fee_c=43.84,
        seasons={
            "peak_season": [
                ("Off-peak", _t(0), _t(11), 4.20),
                ("Saver", _t(11), _t(16), 1.00),
                ("Peak", _t(16), _t(21), 27.86),
                ("Off-peak", _t(21), _t(23, 59), 4.20),
            ],
            "shoulder_season": [
                ("Off-peak", _t(0), _t(11), 4.20),
                ("Saver", _t(11), _t(16), 1.00),
                ("Peak", _t(16), _t(21), 20.80),
                ("Off-peak", _t(21), _t(23, 59), 4.20),
            ],
        },
        season_months={"peak_season": _PRCER_PEAK_SEASON, "shoulder_season": _PRCER_SHOULDER_SEASON},
        feed_in_name="Residential CER Export",
        feed_in_periods=[
            ("Peak export credit", _t(16), _t(21), _PRCER_PEAK_SEASON, 7.00),
            ("Saver export charge", _t(11), _t(16), _PRCER_SAVER_EXPORT_MONTHS, -1.00),
        ],
        source="Powercor 2026-27 Tariff Summary, 7 May 2026",
        remove_when="aemo-to-tariff carries powercor PRCER (purcell-lab/aemo_to_tariff branch powercor-prcer)",
    ),
}


def get(distributor: str, code: str) -> ExtensionTariff | None:
    return EXTENSIONS.get((distributor, code))


def import_codes(distributor: str) -> list[str]:
    """Extension import codes for ``distributor``, in table order."""
    return [code for (d, code), _ext in EXTENSIONS.items() if d == distributor]


def feed_in_codes(distributor: str) -> list[str]:
    """Extension export codes for ``distributor``: the import code, priced two ways."""
    return [code for (d, code), ext in EXTENSIONS.items() if d == distributor and ext.feed_in_periods]


def import_table(distributor: str) -> dict[str, dict[str, Any]]:
    """Extension entries in the library's table shape: code -> {"name": ...}."""
    return {code: {"name": ext.name, "extension": True} for (d, code), ext in EXTENSIONS.items() if d == distributor}


def feed_in_table(distributor: str) -> dict[str, dict[str, Any]]:
    return {
        code: {"name": ext.feed_in_name or ext.name, "extension": True}
        for (d, code), ext in EXTENSIONS.items()
        if d == distributor and ext.feed_in_periods
    }


def spot_to_tariff(
    interval_end: datetime.datetime, distributor: str, code: str, rrp_mwh: float,
    dlf: float = 1.0, mlf: float = 1.0, market: float = 1.0,
) -> float:
    """c/kWh for an import interval, the library's composition: adjusted spot plus network rate."""
    ext = EXTENSIONS[(distributor, code)]
    rate = ext.network_rate(interval_end)
    if rate is None:
        raise ValueError(f"{distributor}/{code}: no period covers {interval_end.isoformat()}")
    return rrp_mwh * dlf * mlf * market / 10 + rate


def spot_to_feed_in_tariff(
    interval_end: datetime.datetime, distributor: str, code: str, rrp_mwh: float,
    dlf: float = 1.0, mlf: float = 1.0, market: float = 1.0,
) -> float:
    """c/kWh paid for an export interval: adjusted spot plus the credit or charge in force."""
    ext = EXTENSIONS[(distributor, code)]
    return rrp_mwh * dlf * mlf * market / 10 + ext.feed_in_adjustment(interval_end)


def get_periods(distributor: str, code: str, interval_time: datetime.datetime | None = None) -> list[Period]:
    return EXTENSIONS[(distributor, code)].periods(interval_time)


def feed_in_periods_for(
    distributor: str, code: str, interval_time: datetime.datetime | None = None,
) -> list[Period]:
    """The export rows in force for the month of ``interval_time`` as 4-tuples."""
    ext = EXTENSIONS[(distributor, code)]
    when = interval_time if interval_time is not None else datetime.datetime.now(ZoneInfo(ext.timezone))
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(ext.timezone))
    month = when.astimezone(ZoneInfo(ext.timezone)).month
    return [(name, start, end, rate) for name, start, end, months, rate in ext.feed_in_periods if month in months]


def get_daily_fee(distributor: str, code: str) -> float:
    return EXTENSIONS[(distributor, code)].daily_fee_c
