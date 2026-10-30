"""The active tariff, read from Amber when it is not set, and the tariff sensors it enables (#260).

The integration builds a sensor for every tariff of every distributor in the
region, and enables the ones in DEFAULT_ENABLED_TARIFFS whichever network the
installation is actually on: ten in VIC1. A household is on one tariff. The
active tariff is now resolved in this order:

* the ``active_tariff`` option, when the user chose one;
* the tariff Amber reports for the site, when an Amber Express site sensor is
  present: its ``network`` attribute mapped to a distributor of the region,
  and the ``tariff`` of its ``general`` channel, accepted only when the
  installed tariff library knows the code;
* otherwise the region's first default-enabled tariff, as before.

With ``only_active_tariff`` on (the default), the tariff sensors of the
region other than the active one and its export partner are disabled in the
entity registry, by the integration. Only a sensor the integration enabled by
default is disabled, and only once: a sensor the user enabled, or re-enabled
after the integration disabled it, is left alone. A sensor the integration
disabled is enabled again when it becomes the active tariff, or when the
option is turned off. Nothing is disabled on a default guess: the active
tariff must come from the option or from Amber.

The Day 2-7 sensors of a tariff that is no longer active are removed from the
registry; they are only ever created for the active tariff, and were left
behind as unavailable entities when it changed.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    ACTIVE_TARIFF_AUTO,
    AMBER_FORECAST_PLATFORMS,
    AMBER_NETWORK_ALIASES,
    CONF_ACTIVE_TARIFF,
    CONF_AMBER_FORECAST_ENTITY,
    CONF_ONLY_ACTIVE_TARIFF,
    DEFAULT_ENABLED_TARIFFS,
    DISTRIBUTOR_DISPLAY_NAMES,
    DOMAIN,
    REGION_DISTRIBUTORS,
)
from .tariff_catalogue import export_programs, import_tariff_codes

_LOGGER = logging.getLogger(__name__)

SOURCE_OPTION = "option"
SOURCE_AMBER = "amber"
SOURCE_DEFAULT = "default"

# The key under the integration's own entity registry options that marks a
# tariff sensor the integration disabled for not being the active tariff.
_MARKER = "disabled_for_active_tariff"


@dataclass(frozen=True)
class ActiveTariff:
    """The tariff the region's day 2-7 sensors follow, and where it came from."""

    distributor: str
    code: str
    source: str
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.distributor}/{self.code}"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def network_distributor(network: str, region: str) -> str | None:
    """The distributor of ``region`` that Amber's ``network`` names, or None.

    Matched on the distributor key, its display name and AMBER_NETWORK_ALIASES,
    ignoring case, spaces and punctuation; then on a prefix of four or more
    characters either way, so "Endeavour" meets "Endeavour Energy".
    """
    wanted = _norm(network)
    if not wanted:
        return None
    candidates = {
        dist: {_norm(dist), _norm(DISTRIBUTOR_DISPLAY_NAMES.get(dist, dist))}
        | {_norm(a) for a, d in AMBER_NETWORK_ALIASES.items() if d == dist}
        for dist in REGION_DISTRIBUTORS.get(region, [])
    }
    for dist, names in candidates.items():
        if wanted in names:
            return dist
    for dist, names in candidates.items():
        for name in names:
            shorter = min(len(name), len(wanted))
            if shorter >= 4 and (name.startswith(wanted) or wanted.startswith(name)):
                return dist
    return None


def amber_site_tariff(attributes: Mapping[str, Any], region: str) -> tuple[str, str] | str:
    """(distributor, code) from an Amber Express site sensor, or why not.

    The ``general`` channel's tariff is the import tariff; the ``feedIn``
    channel reports the same import code, not the export one, so the export
    partner comes from the tariff catalogue instead.
    """
    network = attributes.get("network")
    if not isinstance(network, str) or not network:
        return "no network attribute"
    dist = network_distributor(network, region)
    if dist is None:
        return f"network {network!r} is not a distributor of {region}"
    channels = attributes.get("channels")
    general = [
        c for c in channels if isinstance(c, Mapping) and c.get("type") == "general"
    ] if isinstance(channels, list) else []
    code = str(general[0].get("tariff") or "").strip() if general else ""
    if not code:
        return "no tariff on the general channel"
    if code not in import_tariff_codes(dist):
        return f"tariff {dist}/{code} is not in the installed tariff library"
    return dist, code


def amber_site_entities(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """The enabled Amber sensors whose state carries a site's network and channels.

    With an Amber forecast entity configured, a site sensor of the same Amber
    config entry comes first, for a user with more than one site.
    """
    registry = er.async_get(hass)
    sites = []
    for e in registry.entities.values():
        if e.domain != "sensor" or e.platform not in AMBER_FORECAST_PLATFORMS or e.disabled_by is not None:
            continue
        if e.entity_id.endswith("_site") or _looks_like_site(hass, e.entity_id):
            sites.append(e)
    configured = (getattr(entry, "options", None) or {}).get(CONF_AMBER_FORECAST_ENTITY)
    paired = registry.entities.get(configured) if isinstance(configured, str) and configured else None
    pair_id = getattr(paired, "config_entry_id", None)
    sites.sort(key=lambda e: (getattr(e, "config_entry_id", None) != pair_id or pair_id is None, e.entity_id))
    return [e.entity_id for e in sites]


def _looks_like_site(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and "network" in state.attributes and "channels" in state.attributes


def detect_amber_tariff(hass: HomeAssistant, entry: ConfigEntry, region: str) -> ActiveTariff | None:
    """The tariff the first readable Amber site sensor reports, or None."""
    for entity_id in amber_site_entities(hass, entry):
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            continue
        found = amber_site_tariff(state.attributes, region)
        if isinstance(found, tuple):
            return ActiveTariff(found[0], found[1], SOURCE_AMBER, entity_id)
        _LOGGER.info("Amber site %s: %s; active tariff not taken from Amber", entity_id, found)
    return None


def default_tariff(region: str) -> tuple[str, str] | None:
    """The region's first default-enabled tariff, the fallback since before #260."""
    for dist in REGION_DISTRIBUTORS.get(region, []):
        for code in import_tariff_codes(dist):
            if (dist, code) in DEFAULT_ENABLED_TARIFFS:
                return dist, code
    return None


def resolve_active_tariff(hass: HomeAssistant | None, entry: ConfigEntry, region: str) -> ActiveTariff | None:
    """The option, else Amber, else the region's default."""
    chosen = (getattr(entry, "options", None) or {}).get(CONF_ACTIVE_TARIFF, "")
    if isinstance(chosen, str) and chosen and chosen != ACTIVE_TARIFF_AUTO and "/" in chosen:
        dist, code = chosen.split("/", 1)
        return ActiveTariff(dist, code, SOURCE_OPTION)
    if hass is not None:
        detected = detect_amber_tariff(hass, entry, region)
        if detected is not None:
            return detected
    fallback = default_tariff(region)
    return ActiveTariff(fallback[0], fallback[1], SOURCE_DEFAULT) if fallback else None


def only_active_tariff(entry: ConfigEntry) -> bool:
    return bool((getattr(entry, "options", None) or {}).get(CONF_ONLY_ACTIVE_TARIFF, True))


def _tariff_of(unique_id: str, prefix: str) -> tuple[str, str] | None:
    """(distributor, import code) of a full-horizon tariff sensor's unique id.

    ``{entry}_{region}_{dist}_{code}_tariff`` and ``..._{import code}_export_tariff``;
    distributor keys hold no underscore, codes may.
    """
    if not unique_id.startswith(prefix):
        return None
    rest = unique_id[len(prefix):]
    for suffix in ("_export_tariff", "_tariff"):
        if rest.endswith(suffix):
            dist, _, code = rest[: -len(suffix)].partition("_")
            return (dist, code) if dist and code else None
    return None


def _days27_tariff_of(unique_id: str, region: str) -> tuple[str, str] | None:
    prefix = f"nem_pd7day_{region}_"
    if not unique_id.startswith(prefix):
        return None
    rest = unique_id[len(prefix):]
    for suffix in ("_export_days27", "_days27"):
        if rest.endswith(suffix):
            dist, _, code = rest[: -len(suffix)].partition("_")
            return (dist, code) if dist and code else None
    return None


@dataclass
class SyncResult:
    enabled: list[str]
    disabled: list[str]
    removed: list[str]


def sync_tariff_entities(
    hass: HomeAssistant, entry: ConfigEntry, region: str, active: ActiveTariff | None,
    days27_active: bool,
) -> SyncResult:
    """Enable the active tariff's sensors and disable the others, as the module docstring says."""
    registry = er.async_get(hass)
    integration = er.RegistryEntryDisabler.INTEGRATION
    restrict = only_active_tariff(entry) and active is not None and active.source != SOURCE_DEFAULT
    wanted = (active.distributor, active.code) if active is not None else None
    result = SyncResult([], [], [])
    prefix = f"{entry.entry_id}_{region}_"
    for reg in list(_entries_for(registry, entry)):
        tariff = _tariff_of(reg.unique_id, prefix)
        if tariff is not None:
            _sync_one(registry, reg, tariff, wanted, restrict, integration, result)
            continue
        days27 = _days27_tariff_of(reg.unique_id, region)
        if days27 is not None and (not days27_active or days27 != wanted):
            registry.async_remove(reg.entity_id)
            result.removed.append(reg.entity_id)
    return result


def _entries_for(registry: Any, entry: ConfigEntry) -> Iterable[Any]:
    return [
        e for e in registry.entities.values()
        if getattr(e, "config_entry_id", None) == entry.entry_id and getattr(e, "platform", None) == DOMAIN
    ]


def _sync_one(registry: Any, reg: Any, tariff: tuple[str, str], wanted: tuple[str, str] | None,
              restrict: bool, integration: Any, result: SyncResult) -> None:
    marked = bool(((getattr(reg, "options", None) or {}).get(DOMAIN) or {}).get(_MARKER))
    if tariff == wanted or not restrict:
        # The active tariff, or nothing restricted: undo what the integration
        # disabled, and enable the active one even when it is not a default.
        if reg.disabled_by == integration and (tariff == wanted or marked):
            registry.async_update_entity(reg.entity_id, disabled_by=None)
            result.enabled.append(reg.entity_id)
        if marked:
            registry.async_update_entity_options(reg.entity_id, DOMAIN, {_MARKER: False})
        return
    # Not the active tariff. Leave a sensor the user enabled (a non-default
    # one that is enabled), or re-enabled after it was disabled here.
    if reg.disabled_by is not None or marked or tariff not in DEFAULT_ENABLED_TARIFFS:
        return
    registry.async_update_entity(reg.entity_id, disabled_by=integration)
    registry.async_update_entity_options(reg.entity_id, DOMAIN, {_MARKER: True})
    result.disabled.append(reg.entity_id)


def export_partner(active: ActiveTariff) -> str | None:
    return export_programs(active.distributor).get(active.code)


def amber_loss_factor(hass: HomeAssistant | None) -> float | None:
    """The spot multiplier in the current settled interval of an Amber Express feed-in price.

    Amber Express publishes each interval's price (``per_kwh``) beside the
    spot price with GST (``spot_per_kwh``). Outside an export credit or
    charge window a settled feed-in price is spot without GST times the
    site's loss factor: RRP x 1.0272 on the live Energex site, in every one of
    576 intervals checked against NEMWEB. Forecast intervals follow Amber's
    own prediction, not spot, so only the settled one is read. Shown in the
    options as a suggestion; never applied by itself.
    """
    if hass is None:
        return None
    registry = er.async_get(hass)
    for e in registry.entities.values():
        if e.domain != "sensor" or e.platform not in AMBER_FORECAST_PLATFORMS or "feed_in" not in e.entity_id:
            continue
        state = hass.states.get(e.entity_id)
        rows = state.attributes.get("detailedForecast") if state is not None else None
        settled = next((r for r in rows or () if isinstance(r, Mapping) and r.get("estimate") is False), None)
        if settled is None:
            continue
        try:
            price, spot = float(settled["per_kwh"]), float(settled["spot_per_kwh"])
        except (KeyError, TypeError, ValueError):
            continue
        if spot >= 0.01:  # 1 c/kWh: a ratio of near-zero prices says nothing
            return round(price * 1.1 / spot, 4)  # spot_per_kwh carries GST, per_kwh does not
    return None


def watch_amber_site(hass: HomeAssistant, entry: ConfigEntry, region: str, active: ActiveTariff | None) -> Any:
    """Reload the entry once an Amber site sensor reports a different tariff.

    At startup the Amber integration may not have written its state yet, so
    the default stood in. Only an automatic choice is watched; a tariff set
    in the options is never replaced.
    """
    if active is not None and active.source == SOURCE_OPTION:
        return None
    sites = amber_site_entities(hass, entry)
    if not sites:
        return None
    current = active.key if active is not None else None

    @callback
    def _site_changed(_event: Event[Any]) -> None:
        detected = detect_amber_tariff(hass, entry, region)
        if detected is not None and detected.key != current:
            _LOGGER.info(
                "Amber site %s reports tariff %s (was %s); reloading %s",
                detected.detail, detected.key, current, entry.title,
            )
            unsub()
            hass.config_entries.async_schedule_reload(entry.entry_id)

    unsub = async_track_state_change_event(hass, sites, _site_changed)
    return unsub
