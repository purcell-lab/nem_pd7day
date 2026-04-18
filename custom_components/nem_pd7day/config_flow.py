"""Config flow for NEM PD7DAY integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .const import (
    CONF_REGION,
    CONF_REGIONS,
    DEFAULT_REGION,
    DOMAIN,
    FETCH_TIMES_NEM,
    REGIONS,
)
from .pd7day_client import PD7DayClient

_LOGGER = logging.getLogger(__name__)


class PD7DayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            region = user_input[CONF_REGION]

            try:
                session = async_get_clientsession(self.hass)
                client = PD7DayClient(session)
                await client.fetch_all([region])
            except aiohttp.ClientError as exc:
                _LOGGER.warning("PD7DAY connectivity check failed: %s", exc)
                errors["base"] = "cannot_connect"
            except ValueError as exc:
                _LOGGER.warning("PD7DAY data error: %s", exc)
                errors["base"] = "invalid_data"
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during PD7DAY setup: %s", exc)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id("nem_pd7day")
                self._abort_if_unique_id_configured()
                fetch_times_str = ", ".join(
                    f"{h:02d}:{m:02d}" for h, m in FETCH_TIMES_NEM
                )
                return self.async_create_entry(
                    title=f"NEM PD7DAY {region}",
                    data={CONF_REGION: region},
                    description_placeholders={"fetch_times": fetch_times_str},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_REGION, default=DEFAULT_REGION): selector.selector(
                    {
                        "select": {
                            "options": REGIONS,
                            "multiple": False,
                            "mode": "dropdown",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "fetch_times": ", ".join(
                    f"{h:02d}:{m:02d}" for h, m in FETCH_TIMES_NEM
                )
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return PD7DayOptionsFlow(config_entry)


class PD7DayOptionsFlow(config_entries.OptionsFlow):
    """Allow changing region after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={CONF_REGION: user_input[CONF_REGION]},
            )

        current_region = self._entry.options.get(
            CONF_REGION,
            self._entry.data.get(CONF_REGION) or
            (self._entry.data.get(CONF_REGIONS, [DEFAULT_REGION])[0]
             if isinstance(self._entry.data.get(CONF_REGIONS), list)
             else self._entry.data.get(CONF_REGIONS, DEFAULT_REGION))
        )

        schema = vol.Schema(
            {
                vol.Required(CONF_REGION, default=current_region): selector.selector(
                    {
                        "select": {
                            "options": REGIONS,
                            "multiple": False,
                            "mode": "dropdown",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "fetch_times": ", ".join(
                    f"{h:02d}:{m:02d}" for h, m in FETCH_TIMES_NEM
                )
            },
        )
