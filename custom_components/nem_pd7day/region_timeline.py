"""The region's price timeline for an entity, through the shared forecast hub (#338, #339).

Adapter layer: reads the entry's dispatch price and the shared P5MIN and
pre-dispatch coordinators from ``hass.data``, and keeps what it builds in the
forecast hub, so every entity of a region shares one timeline per source
change and 5-minute interval, and one set of ``forecast`` entries per
timeline. The spot forecast, day 2-7, Price Timeline and tariff sensors read it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, SHARED_P5MIN_KEY, SHARED_PREDISPATCH_KEY
from .nem_time import NEM_TZ, now_nem
from .timeline_forecast import forecast_entries


def _dispatch_point(entry: ConfigEntry, region: str) -> Any:
    """The 5-minute dispatch price held for ``region``, as the timeline's first point, or None."""
    from .dispatch_client import parse_settlement
    from .price_timeline import DispatchPoint

    runtime_data = getattr(entry, "runtime_data", None)
    dispatch = getattr(runtime_data, "dispatch", None)
    price = dispatch.prices.get(region) if dispatch is not None and getattr(dispatch, "prices", None) else None
    if price is None:
        return None
    try:
        end = parse_settlement(price.interval_datetime).replace(tzinfo=NEM_TZ)
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(price.rrp, (int, float)):
        return None
    return DispatchPoint(end, price.rrp, "")


def listen_for_runs(entity: Any, *keys: str) -> None:
    """Write ``entity`` when a shared coordinator named by ``keys`` (P5MIN, pre-dispatch) holds a new run."""
    data = getattr(entity.hass, "data", None)
    domain_data = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
    for key in keys:
        source = domain_data.get(key)
        if source is not None:
            entity.async_on_remove(source.async_add_listener(entity._schedule_warm_state_write))


def _domain_data(sensor: Any) -> Optional[dict]:
    """``hass.data[DOMAIN]``, or None without Home Assistant's data (a bare entity, in tests)."""
    data = getattr(getattr(sensor, "hass", None), "data", None)
    return data.setdefault(DOMAIN, {}) if isinstance(data, dict) else None


def _kept(sensor: Any, kind: str, identity: Any, compute: Any, pin: Any) -> Any:
    """``compute()`` kept in the shared hub while ``identity`` holds; computed each time without a hub."""
    from .forecast_hub import shared_forecast_hub

    domain_data = _domain_data(sensor)
    if domain_data is None:
        return compute()
    return shared_forecast_hub(domain_data).keep(kind, identity, None, compute, pin=pin)


def timeline_forecast(sensor: Any, timeline: Any, calibrated: list[dict]) -> list[dict]:
    """The timeline as ``forecast`` entries, once per timeline for both spot sensors (#339)."""
    return _kept(sensor, "forecast:" + sensor._region, (id(timeline), id(calibrated)),
                 lambda: forecast_entries(timeline, calibrated), pin=(timeline, calibrated))


def region_timeline(sensor: Any, d: Any, calibrated: list[dict]) -> Any:
    """The region's price timeline now, kept in the shared hub until a source or the 5-minute interval moves.

    ``calibrated`` is the region's calibrated PD7DAY forecast, the same list
    the spot forecast sensor publishes, so the timeline costs no calibration.
    """
    from .forecast_hub import shared_forecast_hub
    from .price_timeline import P5_MAX_AGE, PD_MAX_AGE, build_timeline

    domain_data = _domain_data(sensor)

    def run_of(key: str) -> Any:
        """The shared coordinator's run, if it holds one (a run has a real run time)."""
        run = getattr((domain_data or {}).get(key), "latest", None)
        return run if isinstance(getattr(run, "run_at", None), datetime) else None

    p5, pd = run_of(SHARED_P5MIN_KEY), run_of(SHARED_PREDISPATCH_KEY)
    dispatch = _dispatch_point(sensor._entry, sensor._region)
    now = now_nem()

    def build() -> Any:
        return build_timeline(
            now, sensor._region, dispatch=dispatch, p5=p5, pd=pd, pd7day=calibrated,
            pd7day_run_at=d.forecast_generated_at, pd7day_source_file=getattr(d, "source_file", None),
        )

    if domain_data is None:
        return build()
    hub = shared_forecast_hub(domain_data)
    identity = (dispatch, id(p5), getattr(p5, "run_at", None), id(pd), getattr(pd, "run_at", None), id(calibrated))
    key = (
        now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0),
        p5 is not None and now - p5.run_at > P5_MAX_AGE,
        pd is not None and now - pd.run_at > PD_MAX_AGE,
    )
    # The memo pins the inputs its identity names by id, so those ids stay unique.
    return hub.keep("timeline:" + sensor._region, identity, key, build, pin=(p5, pd, calibrated))
