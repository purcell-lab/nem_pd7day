"""Join the day 2-7 series to the Amber forecast it is summed with (#235).

A consumer such as HAEO sums an Amber forecast (day 1) with the day 2-7 series
and reads either series as 0 outside its span. Started by the clock alone, the
day 2-7 series left a hole after Amber's last interval: 30 minutes on most
refreshes and about 9 hours around 12:30 NEM, before Amber extends its
forecast. The series now starts at the end of Amber's coverage instead; see
nem_time.day27_start.

The Amber forecast sensors are found in the entity registry: every enabled
sensor of the Amber Electric and Amber Express integrations. Only those that
carry a readable forecast count, so their price, renewables and descriptor
sensors drop out when read. The amber_forecast_entity option, when set,
replaces the search with that one entity.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import AMBER_FORECAST_PLATFORMS, CONF_AMBER_FORECAST_ENTITY
from .nem_time import Day27Start, day27_start

if TYPE_CHECKING:
    from homeassistant.core import EventStateChangedData
    from homeassistant.helpers.entity import Entity


def amber_entity_id(entry: ConfigEntry) -> str | None:
    """The Amber forecast entity configured as an override, or None."""
    options = getattr(entry, "options", None) or {}
    entity_id = options.get(CONF_AMBER_FORECAST_ENTITY)
    return entity_id if isinstance(entity_id, str) and entity_id else None


def amber_forecast_entities(hass: HomeAssistant, entry: ConfigEntry) -> tuple[str, ...]:
    """The configured override, else every enabled Amber Electric and Amber Express sensor."""
    configured = amber_entity_id(entry)
    if configured is not None:
        return (configured,)
    registry = er.async_get(hass)
    return tuple(sorted(
        e.entity_id for e in registry.entities.values()
        if e.domain == "sensor" and e.platform in AMBER_FORECAST_PLATFORMS and e.disabled_by is None
    ))


def amber_forecasts(hass: HomeAssistant | None, entity_ids: Sequence[str]) -> list[Mapping[str, Any]]:
    """The attributes of each of ``entity_ids`` that is present and available."""
    if hass is None:
        return []
    found = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is not None and state.state != "unavailable":
            found.append(state.attributes)
    return found


def day27_start_for(hass: HomeAssistant | None, entity_ids: Sequence[str]) -> Day27Start:
    """Where the day 2-7 series starts now, given the Amber sensors found at setup."""
    return day27_start(amber_forecasts(hass, entity_ids))


def track_day27_start(entity: "Entity", entry: ConfigEntry, write: Callable[[], None]) -> tuple[str, ...]:
    """Find the Amber forecast sensors and re-write ``entity`` when they move the start.

    Returns the sensors found, for the entity to read on each write. Amber
    updates every 5 minutes but its coverage end moves about every 30, and
    around 12:30 NEM by a day, so writes follow the start, not each update
    (#215). An Amber integration added later is found at the next reload.
    """
    entity_ids = amber_forecast_entities(entity.hass, entry)
    if not entity_ids:
        return ()
    last = [day27_start_for(entity.hass, entity_ids)]

    @callback
    def _amber_changed(_event: Event[EventStateChangedData]) -> None:
        start = day27_start_for(entity.hass, entity_ids)
        if start != last[0]:
            last[0] = start
            write()

    entity.async_on_remove(
        async_track_state_change_event(entity.hass, list(entity_ids), _amber_changed)
    )
    return entity_ids
