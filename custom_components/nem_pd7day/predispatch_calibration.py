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

A period is served by the first of these that serves it:

1. **Pre-dispatch's own stage 1 (#337 stage 3b)**, ``pd_isotonic``: the
   region's fit on its pre-dispatch observations against TradingIS
   (``predispatch_fit``), by horizon band and solar time of day, where the
   period's band, or a shorter one standing in for a thin bucket, was
   admitted by the fit's holdout.
2. **The interim**, ``pd7day_iso_h24_48``: at ``24 <= horizon < 48`` h, the
   region's PD7DAY stage 1 isotonic model, the ``h24_48`` bucket for the
   period's solar time of day, keyed as PD7DAY serves (#208); stage 1 only.
   Raw pre-dispatch at that horizon reads well above the actual price: on 15
   November it put QLD1's first day 2-7 point at 30.2 c where Amber's last
   was about 13 c.
3. **Passthrough**, the RRP itself: below 24 h without an admitted band, and
   wherever no fit is held. PD7DAY's shorter buckets were fitted to PD7DAY's
   error at horizons where pre-dispatch is much better, so they are not used.

The horizon is the period start minus the run time, as stage 3a records it
and as PD7DAY measures its own.

Domain layer: no Home Assistant imports, no clock. The models are passed in
as ``ServingModels``: per region its ``PredispatchRecord`` (``calibration``,
``fit_generation``) and its PD7DAY ``CalibrationStore`` (``apply_to_price``,
``fit_generation``). A plain mapping is the PD7DAY stores alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from .const import DOMAIN
from .nem_time import NEM_TZ

PASSTHROUGH = "passthrough"
INTERIM_SOURCE = "pd7day_iso_h24_48"
INTERIM_MIN_HOURS = 24.0
INTERIM_MAX_HOURS = 48.0
PERIOD = timedelta(minutes=30)

# hass.data[DOMAIN] keys of the {region: CalibrationStore} and {region: PredispatchRecord}
# registries, kept by __init__.
CALIBRATION_STORES_KEY = "calibration_stores"
PD_RECORDS_KEY = "pd_records"
# Stage 3b's label, as predispatch_fit.PD_SOURCE (kept here so serving needs no fit import).
PD_SOURCE = "pd_isotonic"


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


@dataclass(frozen=True)
class ServingModels:
    """What pre-dispatch is served through, by region: its own fit, and PD7DAY's for the interim."""

    pd7day: Mapping[str, Any] = field(default_factory=dict)   # region -> CalibrationStore
    pd: Mapping[str, Any] = field(default_factory=dict)       # region -> PredispatchRecord


def _as_models(models: Any) -> ServingModels:
    """A ServingModels; a plain mapping is the PD7DAY stores alone, as the interim took them."""
    if isinstance(models, ServingModels):
        return models
    return ServingModels(pd7day=models if isinstance(models, Mapping) else {})


def models_from(domain_data: Any) -> ServingModels:
    """The registries in ``hass.data[DOMAIN]``; empty without them."""
    def held(key: str) -> Mapping[str, Any]:
        found = domain_data.get(key) if isinstance(domain_data, Mapping) else None
        return found if isinstance(found, Mapping) else {}

    return ServingModels(pd7day=held(CALIBRATION_STORES_KEY), pd=held(PD_RECORDS_KEY))


def models_for(entity: Any) -> ServingModels:
    """The registries an entity's Home Assistant holds; empty for an entity not added yet."""
    data = getattr(getattr(entity, "hass", None), "data", None)
    return models_from(data.get(DOMAIN) if isinstance(data, Mapping) else None)


def generation(region: str, models: Any) -> tuple[Optional[int], Optional[int]]:
    """The region's PD7DAY and pre-dispatch fit generations, for memo keys: a refit changes what is served."""
    held = _as_models(models)

    def of(model: Any) -> Optional[int]:
        value = getattr(model, "fit_generation", None)
        return value if isinstance(value, int) else None

    return of(held.pd7day.get(region)), of(held.pd.get(region))


def _mwh(value: Any) -> Optional[float]:
    return round(value * 1000.0, 3) if isinstance(value, (int, float)) else None


def _interim_result(pd7day_store: Any, raw_kwh: float, horizon: float, start: datetime) -> Optional[dict]:
    """PD7DAY's h24_48 stage 1 result for a period, or None where the interim does not serve it."""
    if pd7day_store is None or not INTERIM_MIN_HOURS <= horizon < INTERIM_MAX_HOURS:
        return None
    try:
        cal = pd7day_store.apply_to_price(raw_kwh, horizon, start.hour, interval_dt=start)
    except (AttributeError, TypeError, ValueError):
        return None
    calibrated = cal.get("calibrated") if isinstance(cal, dict) else None
    if not isinstance(calibrated, (int, float)) or cal.get("calibrated_source") in (None, PASSTHROUGH):
        return None
    return cal


def interim_kwh(pd7day_store: Any, raw_kwh: float, horizon: float, start: datetime) -> Optional[float]:
    """The interim's $/kWh for a period, None where it would not serve: stage 3b's holdout baseline."""
    cal = _interim_result(pd7day_store, raw_kwh, horizon, start)
    return float(cal["calibrated"]) if cal is not None else None


def _served(cal: dict, source: str, horizon: float, rrp_mwh: float) -> ServedPeriod:
    return ServedPeriod(
        round(cal["calibrated"] * 1000.0, 3), source, horizon, rrp_mwh,
        _mwh(cal.get("p10")), _mwh(cal.get("p50")), _mwh(cal.get("p90")),
        cal.get("band_source"), cal.get("n_obs"),
    )


def serve(
    region: str, end: datetime, rrp_mwh: Optional[float],
    run_at: Optional[datetime] = None, models: Any = None,
) -> ServedPeriod:
    """The served pre-dispatch RRP for ``region``'s period ending ``end``, and how it was served.

    Pre-dispatch's own fit where a band is admitted for the period (stage 3b),
    else PD7DAY's h24_48 from 24 h (the interim), else passthrough.
    """
    horizon = horizon_hours(end, run_at)
    passthrough = ServedPeriod(rrp_mwh, PASSTHROUGH, horizon, rrp_mwh)
    if rrp_mwh is None or horizon is None:
        return passthrough
    held = _as_models(models)
    start = (end - PERIOD).astimezone(NEM_TZ)
    fit = getattr(held.pd.get(region), "calibration", None)
    if fit is not None:
        cal = fit.apply(rrp_mwh / 1000.0, horizon, start)
        if cal is not None:
            return _served(cal, PD_SOURCE, horizon, rrp_mwh)
    cal = _interim_result(held.pd7day.get(region), rrp_mwh / 1000.0, horizon, start)
    if cal is not None:
        return _served(cal, INTERIM_SOURCE, horizon, rrp_mwh)
    return passthrough


def calibrated_rrp_mwh(
    region: str, end: datetime, rrp_mwh: Optional[float],
    run_at: Optional[datetime] = None, models: Any = None,
) -> Optional[float]:
    """The served pre-dispatch RRP ($/MWh) for ``region``'s period ending ``end``."""
    return serve(region, end, rrp_mwh, run_at, models).rrp_mwh
