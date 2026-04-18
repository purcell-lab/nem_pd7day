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
    AMBER_ACTUAL_ENTITY,
    CONF_AMBER_SENSOR,
    CONF_CALIBRATION_REGION,
    CONF_REGIONS,
    DEFAULT_CALIBRATION_REGION,
    DEFAULT_REGIONS,
    DOMAIN,
    FETCH_TIMES_NEM,
    REGIONS,
)
from .pd7day_client import PD7DayClient

_LOGGER = logging.getLogger(__name__)


class PD7DayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending_regions: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_regions = user_input[CONF_REGIONS]
            if isinstance(selected_regions, str):
                regions = [selected_regions]
            else:
                regions = list(selected_regions)

            if not regions:
                errors[CONF_REGIONS] = "required"
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_REGIONS, default=DEFAULT_REGIONS
                            ): selector.selector(
                                {
                                    "select": {
                                        "options": REGIONS,
                                        "multiple": True,
                                        "mode": "list",
                                    }
                                }
                            ),
                        }
                    ),
                    errors=errors,
                    description_placeholders={
                        "fetch_times": ", ".join(
                            f"{h:02d}:{m:02d}" for h, m in FETCH_TIMES_NEM
                        )
                    },
                )

            try:
                session = async_get_clientsession(self.hass)
                client = PD7DayClient(session)
                await client.fetch_all(regions[:1])
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
                self._pending_regions = regions
                return await self.async_step_calibration()

        schema = vol.Schema(
            {
                vol.Required(CONF_REGIONS, default=DEFAULT_REGIONS): selector.selector(
                    {
                        "select": {
                            "options": REGIONS,
                            "multiple": True,
                            "mode": "list",
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

    async def async_step_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Collect calibration-specific options after region selection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            calibration_region = user_input.get(CONF_CALIBRATION_REGION)
            amber_sensor = user_input.get(CONF_AMBER_SENSOR, "")

            if calibration_region not in self._pending_regions:
                errors[CONF_CALIBRATION_REGION] = "invalid_option"
            else:
                await self.async_set_unique_id("nem_pd7day")
                self._abort_if_unique_id_configured()
                fetch_times_str = ", ".join(
                    f"{h:02d}:{m:02d}" for h, m in FETCH_TIMES_NEM
                )
                return self.async_create_entry(
                    title="NEM PD7DAY",
                    data={
                        CONF_REGIONS: self._pending_regions,
                        CONF_CALIBRATION_REGION: calibration_region,
                        CONF_AMBER_SENSOR: amber_sensor,
                    },
                    description_placeholders={"fetch_times": fetch_times_str},
                )

        default_region = (
            self._pending_regions[0]
            if self._pending_regions
            else DEFAULT_CALIBRATION_REGION
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CALIBRATION_REGION,
                    default=default_region,
                ): selector.selector(
                    {
                        "select": {
                            "options": self._pending_regions or DEFAULT_REGIONS,
                            "multiple": False,
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Optional(
                    CONF_AMBER_SENSOR,
                    default="",
                ): selector.selector(
                    {
                        "entity": {
                            "domain": "sensor",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="calibration",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return PD7DayOptionsFlow(config_entry)


class PD7DayOptionsFlow(config_entries.OptionsFlow):
    """Allow changing regions after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._pending_regions = self._entry.options.get(
            CONF_REGIONS,
            self._entry.data.get(CONF_REGIONS, DEFAULT_REGIONS),
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            selected_regions = user_input[CONF_REGIONS]
            if isinstance(selected_regions, str):
                self._pending_regions = [selected_regions]
            else:
                self._pending_regions = list(selected_regions)

            if not self._pending_regions:
                schema = vol.Schema(
                    {
                        vol.Required(
                            CONF_REGIONS,
                            default=DEFAULT_REGIONS,
                        ): selector.selector(
                            {
                                "select": {
                                    "options": REGIONS,
                                    "multiple": True,
                                    "mode": "list",
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
            return await self.async_step_calibration()

        current_regions = self._entry.options.get(
            CONF_REGIONS,
            self._entry.data.get(CONF_REGIONS, DEFAULT_REGIONS),
        )

        schema = vol.Schema(
            {
                vol.Required(CONF_REGIONS, default=current_regions): selector.selector(
                    {
                        "select": {
                            "options": REGIONS,
                            "multiple": True,
                            "mode": "list",
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

    async def async_step_calibration(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Configure calibration region and Amber sensor for options flow."""
        current_calibration_region = self._entry.options.get(
            CONF_CALIBRATION_REGION,
            self._entry.data.get(
                CONF_CALIBRATION_REGION,
                self._pending_regions[0] if self._pending_regions else DEFAULT_CALIBRATION_REGION,
            ),
        )
        current_amber_sensor = self._entry.options.get(
            CONF_AMBER_SENSOR,
            self._entry.data.get(CONF_AMBER_SENSOR, ""),
        )

        if current_calibration_region not in self._pending_regions:
            current_calibration_region = (
                self._pending_regions[0]
                if self._pending_regions
                else DEFAULT_CALIBRATION_REGION
            )

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_REGIONS: self._pending_regions,
                    CONF_CALIBRATION_REGION: user_input[CONF_CALIBRATION_REGION],
                    CONF_AMBER_SENSOR: user_input.get(CONF_AMBER_SENSOR, ""),
                },
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CALIBRATION_REGION,
                    default=current_calibration_region,
                ): selector.selector(
                    {
                        "select": {
                            "options": self._pending_regions,
                            "multiple": False,
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Optional(
                    CONF_AMBER_SENSOR,
                    default=current_amber_sensor,
                ): selector.selector(
                    {
                        "entity": {
                            "domain": "sensor",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="calibration",
            data_schema=schema,
        )
