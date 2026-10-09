"""Join the day 2-7 series to the Amber forecast it is summed with (#235).

A consumer such as HAEO sums an Amber forecast (day 1) with the day 2-7 series
and reads either series as 0 outside its span. Started by the clock alone, the
day 2-7 series left a hole after Amber's last interval: 30 minutes on most
refreshes and about 9 hours around 12:30 NEM, before Amber extends its
forecast. With an Amber forecast entity configured, the series starts at the
end of Amber's coverage instead; see nem_time.day27_start.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_AMBER_FORECAST_ENTITY
from .nem_time import Day27Start, day27_start

if TYPE_CHECKING:
    from homeassistant.core import EventStateChangedData
    from homeassistant.helpers.entity import Entity


def amber_entity_id(entry: ConfigEntry) -> str | None:
    """The configured Amber forecast entity, or None."""
    options = getattr(entry, "options", None) or {}
    entity_id = options.get(CONF_AMBER_FORECAST_ENTITY)
    return entity_id if isinstance(entity_id, str) and entity_id else None


def amber_attributes(hass: HomeAssistant | None, entry: ConfigEntry) -> Mapping[str, Any] | None:
    """The configured Amber entity's attributes, or None when it is unset or unavailable."""
    entity_id = amber_entity_id(entry)
    if hass is None or entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state == "unavailable":
        return None
    return state.attributes


def day27_start_for(hass: HomeAssistant | None, entry: ConfigEntry) -> Day27Start:
    """Where this entry's day 2-7 series starts now."""
    return day27_start(amber_attributes(hass, entry))


def track_day27_start(entity: "Entity", entry: ConfigEntry, write: Callable[[], None]) -> None:
    """Re-write ``entity`` when the Amber forecast moves the day 2-7 start.

    Amber updates every 5 minutes but its coverage end moves about every 30,
    and around 12:30 NEM by a day, so writes follow the start, not each
    update (#215).
    """
    entity_id = amber_entity_id(entry)
    if entity_id is None:
        return
    last = [day27_start_for(entity.hass, entry)]

    @callback
    def _amber_changed(_event: Event[EventStateChangedData]) -> None:
        start = day27_start_for(entity.hass, entry)
        if start != last[0]:
            last[0] = start
            write()

    entity.async_on_remove(
        async_track_state_change_event(entity.hass, [entity_id], _amber_changed)
    )
