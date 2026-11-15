"""Pre-dispatch RRP as the timeline serves it (#333 decision 3, #337, #339).

Every consumer reads the served price from ``serve`` (or its shorthand
``calibrated_rrp_mwh``), so a change here reaches them all at once and none
can drift:

- the price timeline's ``predispatch`` points, and through it the spot
  forecast, day 2-7, tariff and chart entities;
- the node and zone sensors' ``predispatch_forecast`` entries, as
  ``calibrated_rrp_mwh`` beside the raw-RRP price they publish. They stay on
  raw RRP because the local adjustment comes from the same NEMDE run;
- the pre-dispatch constraints sensor's ``calibrated_rrp`` beside ``rrp``.

**Interim, until stage 3b fits pre-dispatch's own calibration (#337).** Raw
pre-dispatch at 24 h and beyond reads well above the actual price: on 15
November it put QLD1's first day 2-7 point at 30.2 c where Amber's last was
about 13 c. Periods at ``24 <= horizon < 48`` h are served through the
region's PD7DAY stage 1 isotonic model, the ``h24_48`` bucket for the
period's solar time of day, the same keying PD7DAY serves with (#208). Stage
1 only: no STPASA stage 2 and no run features. They are labelled
``pd7day_iso_h24_48``, never to be taken for a pre-dispatch fit.

Below 24 h a period stays ``passthrough``: it sits under Amber for every
consumer that cuts at Amber, and PD7DAY's shorter buckets were fitted to
PD7DAY's error at horizons where pre-dispatch is much better. Without a
PD7DAY fit for the region (or a bucket without one) it is ``passthrough``.

The horizon is the period start minus the run time, as stage 3a records it
and as PD7DAY measures its own.

Domain layer: no Home Assistant imports, no clock. The models are passed in:
``models`` maps a region to its PD7DAY ``CalibrationStore`` (anything with
``apply_to_price`` and ``fit_generation``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from .const import DOMAIN
from .nem_time import NEM_TZ

PASSTHROUGH = "passthrough"
INTERIM_SOURCE = "pd7day_iso_h24_48"
INTERIM_MIN_HOURS = 24.0
INTERIM_MAX_HOURS = 48.0
PERIOD = timedelta(minutes=30)

# hass.data[DOMAIN] key of the {region: CalibrationStore} registry, kept by __init__.
CALIBRATION_STORES_KEY = "calibration_stores"


@dataclass(frozen=True)
class ServedPeriod:
    """One pre-dispatch period as served. Prices in $/MWh."""

    rrp_mwh: Optional[float]
    calibrated_source: str
    horizon_hours: Optional[float]
    raw_mwh: Optional[float]
    p10_mwh: Optional[float] = None
    p50_mwh: Optional[float] = None
    p90_mwh: Optional[float] = None
    band_source: Optional[str] = None
    n_obs: Optional[int] = None


def horizon_hours(end: datetime, run_at: Optional[datetime]) -> Optional[float]:
    """Hours from the run to the start of the period ending ``end``; None without a run time."""
    if run_at is None:
        return None
    return round(((end - PERIOD) - run_at).total_seconds() / 3600, 2)


def models_from(domain_data: Any) -> Mapping[str, Any]:
    """The {region: CalibrationStore} registry in ``hass.data[DOMAIN]``; empty without one."""
    found = domain_data.get(CALIBRATION_STORES_KEY) if isinstance(domain_data, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def models_for(entity: Any) -> Mapping[str, Any]:
    """The registry an entity's Home Assistant holds; empty for an entity not added yet."""
    data = getattr(getattr(entity, "hass", None), "data", None)
    return models_from(data.get(DOMAIN) if isinstance(data, Mapping) else None)


def generation(region: str, models: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The region's PD7DAY fit generation, for memo keys: a refit changes what is served."""
    model = models.get(region) if models else None
    value = getattr(model, "fit_generation", None)
    return value if isinstance(value, int) else None


def _mwh(value: Any) -> Optional[float]:
    return round(value * 1000.0, 3) if isinstance(value, (int, float)) else None


def serve(
    region: str, end: datetime, rrp_mwh: Optional[float],
    run_at: Optional[datetime] = None, models: Optional[Mapping[str, Any]] = None,
) -> ServedPeriod:
    """The served pre-dispatch RRP for ``region``'s period ending ``end``, and how it was served."""
    horizon = horizon_hours(end, run_at)
    passthrough = ServedPeriod(rrp_mwh, PASSTHROUGH, horizon, rrp_mwh)
    model = models.get(region) if models else None
    if (rrp_mwh is None or horizon is None or model is None
            or not INTERIM_MIN_HOURS <= horizon < INTERIM_MAX_HOURS):
        return passthrough
    start = (end - PERIOD).astimezone(NEM_TZ)
    try:
        cal = model.apply_to_price(rrp_mwh / 1000.0, horizon, start.hour, interval_dt=start)
    except (AttributeError, TypeError, ValueError):
        return passthrough
    calibrated = cal.get("calibrated") if isinstance(cal, dict) else None
    if not isinstance(calibrated, (int, float)) or cal.get("calibrated_source") in (None, PASSTHROUGH):
        return passthrough
    return ServedPeriod(
        round(calibrated * 1000.0, 3), INTERIM_SOURCE, horizon, rrp_mwh,
        _mwh(cal.get("p10")), _mwh(cal.get("p50")), _mwh(cal.get("p90")),
        cal.get("band_source"), cal.get("n_obs"),
    )


def calibrated_rrp_mwh(
    region: str, end: datetime, rrp_mwh: Optional[float],
    run_at: Optional[datetime] = None, models: Optional[Mapping[str, Any]] = None,
) -> Optional[float]:
    """The served pre-dispatch RRP ($/MWh) for ``region``'s period ending ``end``."""
    return serve(region, end, rrp_mwh, run_at, models).rrp_mwh
