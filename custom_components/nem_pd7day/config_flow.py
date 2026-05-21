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
    CONF_ACTIVE_TARIFF,
    CONF_FORECAST_MODE,
    CONF_REGION,
    CONF_REGIONS,
    DEFAULT_ENABLED_TARIFFS,
    DEFAULT_REGION,
    DISTRIBUTOR_DISPLAY_NAMES,
    DISTRIBUTOR_TARIFFS,
    DOMAIN,
    FETCH_TIMES_NEM,
    FORECAST_MODE_DAYS_2_7,
    FORECAST_MODE_FULL,
    REGION_DISTRIBUTORS,
    REGIONS,
    TARIFF_NAMES,
)
from .pd7day_client import PD7DayClient

_LOGGER = logging.getLogger(__name__)

FORECAST_MODE_OPTIONS = [
    {"value": FORECAST_MODE_FULL, "label": "Full (days 1-7)"},
    {"value": FORECAST_MODE_DAYS_2_7, "label": "Days 2-7 only"},
]


def _tariff_options_for_region(region: str) -> list[dict[str, str]]:
    """Build tariff dropdown options for a region using DEFAULT_ENABLED_TARIFFS."""
    options = []
    for distributor in REGION_DISTRIBUTORS.get(region, []):
        for tariff_code in DISTRIBUTOR_TARIFFS.get(distributor, []):
            if (distributor, tariff_code) not in DEFAULT_ENABLED_TARIFFS:
                continue
            display_name = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
            tariff_name = TARIFF_NAMES.get(distributor, {}).get(tariff_code, tariff_code)
            label = f"{display_name} {tariff_name}"
            options.append({"value": f"{distributor}/{tariff_code}", "label": label})
    return options


def _default_tariff_for_region(region: str) -> str | None:
    """Return the first default-enabled tariff key for the region."""
    for distributor in REGION_DISTRIBUTORS.get(region, []):
        for tariff_code in DISTRIBUTOR_TARIFFS.get(distributor, []):
            if (distributor, tariff_code) in DEFAULT_ENABLED_TARIFFS:
                return f"{distributor}/{tariff_code}"
    return None


class PD7DayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup UI."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        super().__init__()
        self._region: str | None = None

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
                self._region = region
                return await self.async_step_forecast_mode()

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

    async def async_step_forecast_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2: choose forecast mode (active tariff set via Options after setup)."""
        region = self._region or DEFAULT_REGION

        if user_input is not None:
            mode = user_input.get(CONF_FORECAST_MODE, FORECAST_MODE_FULL)

            await self.async_set_unique_id(f"nem_pd7day_{region}")
            self._abort_if_unique_id_configured()
            fetch_times_str = ", ".join(
                f"{h:02d}:{m:02d}" for h, m in FETCH_TIMES_NEM
            )
            return self.async_create_entry(
                title=f"NEM PD7DAY {region}",
                data={CONF_REGION: region},
                options={
                    CONF_REGION: region,
                    CONF_FORECAST_MODE: mode,
                    CONF_ACTIVE_TARIFF: "",
                },
                description_placeholders={"fetch_times": fetch_times_str},
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FORECAST_MODE, default=FORECAST_MODE_FULL
                ): selector.selector(
                    {
                        "select": {
                            "options": FORECAST_MODE_OPTIONS,
                            "multiple": False,
                            "mode": "dropdown",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="forecast_mode",
            data_schema=schema,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return PD7DayOptionsFlow(config_entry)


class PD7DayOptionsFlow(config_entries.OptionsFlow):
    """Allow changing region, forecast mode, and active tariff after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_REGION: user_input[CONF_REGION],
                    CONF_FORECAST_MODE: user_input.get(
                        CONF_FORECAST_MODE, FORECAST_MODE_DAYS_2_7
                    ),
                    CONF_ACTIVE_TARIFF: user_input.get(CONF_ACTIVE_TARIFF, ""),
                },
            )

        current_region = self._entry.options.get(
            CONF_REGION,
            self._entry.data.get(CONF_REGION) or
            (self._entry.data.get(CONF_REGIONS, [DEFAULT_REGION])[0]
             if isinstance(self._entry.data.get(CONF_REGIONS), list)
             else self._entry.data.get(CONF_REGIONS, DEFAULT_REGION))
        )
        current_mode = self._entry.options.get(
            CONF_FORECAST_MODE, FORECAST_MODE_DAYS_2_7
        )
        current_tariff = self._entry.options.get(CONF_ACTIVE_TARIFF, "")

        tariff_options = _tariff_options_for_region(current_region)
        default_tariff = current_tariff or _default_tariff_for_region(current_region) or ""

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
                vol.Required(
                    CONF_FORECAST_MODE, default=current_mode
                ): selector.selector(
                    {
                        "select": {
                            "options": FORECAST_MODE_OPTIONS,
                            "multiple": False,
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Optional(
                    CONF_ACTIVE_TARIFF, default=default_tariff
                ): selector.selector(
                    {
                        "select": {
                            "options": tariff_options,
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
