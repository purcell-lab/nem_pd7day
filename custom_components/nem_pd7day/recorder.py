"""
Recorder platform for nem_pd7day.

Home Assistant's recorder discovers a per-domain ``recorder.py`` exposing
``exclude_attributes(hass)``. The returned attribute names are stripped from the
state attributes before they are written to the recorder database, while the
state itself is still recorded.

This prevents large list-valued attributes from being persisted on every state
change, which otherwise triggers the recorder's 16 KB attribute-size warnings.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback


@callback
def exclude_attributes(hass: HomeAssistant) -> set[str]:
    """Return the nem_pd7day state attributes to exclude from the recorder."""
    return {
        "forecast",
        "forecast_description",
        "slots",              # ToD Stats: fixed 48-slot list
        "notices",            # Grid Notices: unbounded active-notice list
        "active_notices_7d",  # Grid Stress: unbounded 7-day notice list
    }
