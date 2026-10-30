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

from .active_tariff import default_tariff, detect_amber_tariff
from .const import (
    ACTIVE_TARIFF_AUTO,
    AMBER_FORECAST_PLATFORMS,
    CONF_ACTIVE_TARIFF,
    CONF_AMBER_FORECAST_ENTITY,
    CONF_FORECAST_MODE,
    CONF_LOCAL_PRICE_DUIDS,
    CONF_ONLY_ACTIVE_TARIFF,
    CONF_REGION,
    CONF_REGIONS,
    DEFAULT_ENABLED_TARIFFS,
    DEFAULT_REGION,
    DISTRIBUTOR_DISPLAY_NAMES,
    DOMAIN,
    FETCH_TIMES_NEM,
    FORECAST_MODE_DAYS_2_7,
    FORECAST_MODE_FULL,
    REGION_DISTRIBUTORS,
    REGIONS,
)
from .duid_catalogue import Catalogue, load_catalogue
from .local_price import parse_duids
from .pd7day_client import PD7DayClient
from .tariff_catalogue import import_tariff_codes, tariff_name as catalogue_tariff_name

_LOGGER = logging.getLogger(__name__)

FORECAST_MODE_OPTIONS = [
    {"value": FORECAST_MODE_FULL, "label": "Full (days 1-7)"},
    {"value": FORECAST_MODE_DAYS_2_7, "label": "Days 2-7 only"},
]


def _tariff_options_for_region(region: str) -> list[dict[str, str]]:
    """Build tariff dropdown options for a region using DEFAULT_ENABLED_TARIFFS."""
    options = []
    for distributor in REGION_DISTRIBUTORS.get(region, []):
        for tariff_code in import_tariff_codes(distributor):
            if (distributor, tariff_code) not in DEFAULT_ENABLED_TARIFFS:
                continue
            display_name = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
            tariff_name = catalogue_tariff_name(distributor, tariff_code)
            label = f"{display_name} {tariff_name}"
            options.append({"value": f"{distributor}/{tariff_code}", "label": label})
    return options


def _default_tariff_for_region(region: str) -> str | None:
    """Return the first default-enabled tariff key for the region."""
    found = default_tariff(region)
    return f"{found[0]}/{found[1]}" if found else None


def _tariff_label(distributor: str, tariff_code: str) -> str:
    display_name = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
    return f"{display_name} {catalogue_tariff_name(distributor, tariff_code)}"


def _active_tariff_options(region: str, amber: tuple[str, str] | None, current: str) -> list[dict[str, str]]:
    """Automatic first, then the region's default-enabled tariffs (#260).

    The tariff Amber reports, and a current choice, are offered even when
    they are not default-enabled, so they can be kept or picked.
    """
    auto = "Automatic: from Amber, else the region default"
    if amber is not None:
        auto = f"Automatic: {_tariff_label(*amber)} (from Amber)"
    options = [{"value": ACTIVE_TARIFF_AUTO, "label": auto}]
    listed = _tariff_options_for_region(region)
    values = {o["value"] for o in listed}
    for extra in (f"{amber[0]}/{amber[1]}" if amber else "", current):
        if extra and extra != ACTIVE_TARIFF_AUTO and "/" in extra and extra not in values:
            listed.append({"value": extra, "label": _tariff_label(*extra.split("/", 1))})
            values.add(extra)
    return options + listed


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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Allow changing the region for an existing entry (reconfigure flow)."""
        reconfigure_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            region = user_input[CONF_REGION]
            new_unique_id = f"nem_pd7day_{region}"

            # Reject the region only if a *different* entry already owns it.
            for entry in self._async_current_entries(include_ignore=False):
                if (
                    entry.entry_id != reconfigure_entry.entry_id
                    and entry.unique_id == new_unique_id
                ):
                    return self.async_abort(reason="already_configured")

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
                _LOGGER.exception("Unexpected error during PD7DAY reconfigure: %s", exc)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(new_unique_id)
                new_options = {
                    **reconfigure_entry.options,
                    CONF_REGION: region,
                }
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    title=f"NEM PD7DAY {region}",
                    data={CONF_REGION: region},
                    options=new_options,
                    unique_id=new_unique_id,
                )

        current_region = reconfigure_entry.options.get(
            CONF_REGION, reconfigure_entry.data.get(CONF_REGION, DEFAULT_REGION)
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
            step_id="reconfigure",
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


def _amber_forecast_field(current: str) -> dict[Any, Any]:
    """An optional override for the Amber forecast the day 2-7 series joins (#235).

    Empty, the Amber Electric and Amber Express sensors are found in the
    entity registry. Only their own sensors are offered: a wrapper template's
    last point is not an interval start. The current value is suggested
    rather than defaulted, so it can be cleared.
    """
    return {
        vol.Optional(
            CONF_AMBER_FORECAST_ENTITY,
            description={"suggested_value": current or None},
        ): selector.selector(
            {
                "entity": {
                    "filter": [
                        {"integration": platform, "domain": "sensor"}
                        for platform in AMBER_FORECAST_PLATFORMS
                    ],
                }
            }
        ),
    }


async def _async_catalogue(hass: Any) -> Catalogue:
    """The bundled DUID catalogue, read in the executor (it is a file read)."""
    if hass is None:
        return load_catalogue()
    return await hass.async_add_executor_job(load_catalogue)


def _duid_choices(catalogue: Catalogue, region: str, current: list[str]) -> list[dict[str, str]]:
    """The region's DUIDs, most constrained first, and any chosen DUID the catalogue lacks."""
    choices = [{"value": i.duid, "label": i.label} for i in catalogue.region_duids(region)]
    listed = {c["value"] for c in choices}
    return choices + [{"value": d, "label": d} for d in current if d not in listed]


def _local_price_fields(options: dict[str, Any], catalogue: Catalogue, region: str) -> dict[Any, Any]:
    """DUIDs whose local price adjustment is published (#275).

    A dropdown of the region's DUIDs from the bundled catalogue; a DUID it
    does not list, such as one registered since, can still be typed.
    """
    current, _invalid = parse_duids(options.get(CONF_LOCAL_PRICE_DUIDS, ""))
    return {
        vol.Optional(
            CONF_LOCAL_PRICE_DUIDS,
            description={"suggested_value": current or None},
        ): selector.selector({
            "select": {
                "options": _duid_choices(catalogue, region, current),
                "multiple": True,
                "custom_value": True,
                "mode": "dropdown",
            }
        }),
    }


def _options_data(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The options to store, and the form errors: a DUID that cannot be one is refused."""
    duids, invalid = parse_duids(user_input.get(CONF_LOCAL_PRICE_DUIDS, ""))
    if invalid:
        return {}, {CONF_LOCAL_PRICE_DUIDS: "invalid_duid"}
    return {
        CONF_REGION: user_input[CONF_REGION],
        CONF_FORECAST_MODE: user_input.get(CONF_FORECAST_MODE, FORECAST_MODE_DAYS_2_7),
        CONF_ACTIVE_TARIFF: user_input.get(CONF_ACTIVE_TARIFF, ACTIVE_TARIFF_AUTO),
        CONF_ONLY_ACTIVE_TARIFF: user_input.get(CONF_ONLY_ACTIVE_TARIFF, True),
        CONF_AMBER_FORECAST_ENTITY: user_input.get(CONF_AMBER_FORECAST_ENTITY, ""),
        CONF_LOCAL_PRICE_DUIDS: ", ".join(duids),
    }, {}


def _only_active_field(current: bool) -> dict[Any, Any]:
    """Disable the region's other tariff sensors (#260)."""
    return {vol.Optional(CONF_ONLY_ACTIVE_TARIFF, default=current): selector.selector({"boolean": {}})}


class PD7DayOptionsFlow(config_entries.OptionsFlow):
    """Allow changing region, forecast mode, and active tariff after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    def _amber_tariff(self, region: str) -> tuple[str, str] | None:
        """The tariff an Amber site sensor reports, offered as the automatic choice."""
        hass = getattr(self, "hass", None)
        found = detect_amber_tariff(hass, self._entry, region) if hass is not None else None
        return (found.distributor, found.code) if found else None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _options_data(user_input)
            if not errors:
                return self.async_create_entry(title="", data=data)

        current_region = self._entry.options.get(
            CONF_REGION,
            self._entry.data.get(CONF_REGION) or
            (self._entry.data.get(CONF_REGIONS, [DEFAULT_REGION])[0]
             if isinstance(self._entry.data.get(CONF_REGIONS), list)
             else self._entry.data.get(CONF_REGIONS, DEFAULT_REGION))
        )
        current_mode = self._entry.options.get(CONF_FORECAST_MODE, FORECAST_MODE_DAYS_2_7)
        current_tariff = self._entry.options.get(CONF_ACTIVE_TARIFF, "") or ACTIVE_TARIFF_AUTO
        tariff_options = _active_tariff_options(current_region, self._amber_tariff(current_region), current_tariff)
        catalogue = await _async_catalogue(getattr(self, "hass", None))

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
                    CONF_ACTIVE_TARIFF, default=current_tariff
                ): selector.selector(
                    {
                        "select": {
                            "options": tariff_options,
                            "multiple": False,
                            "mode": "dropdown",
                        }
                    }
                ),
                **_only_active_field(self._entry.options.get(CONF_ONLY_ACTIVE_TARIFF, True)),
                **_amber_forecast_field(self._entry.options.get(CONF_AMBER_FORECAST_ENTITY, "")),
                **_local_price_fields(dict(self._entry.options), catalogue, current_region),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "fetch_times": ", ".join(
                    f"{h:02d}:{m:02d}" for h, m in FETCH_TIMES_NEM
                )
            },
        )
