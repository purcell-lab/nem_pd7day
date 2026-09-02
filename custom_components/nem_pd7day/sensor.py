"""
NEM PD7DAY sensor platform.

Sensors
-------
PD7DayForecastSensor          -- regional spot price, calibrated + confidence interval
PD7DayInterconnectorSensor    -- interconnector MW flow + constraint forecast
PD7DayCalibrationSensor       -- calibration status, observation count, MAE by bucket
PD7DayTodSensor               -- time-of-day actual price stats (mean/spread per 30-min slot)
NemPd7dayGridNoticesSensor    -- active MSL/LOR market notice count + structured attributes
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from .nem_time import _amber_express_cutoff, now_nem, parse_iso, to_nem_iso

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import tod_stats as _tod_stats
from .const import (
    ATTR_ATTRIBUTION,
    ATTR_CAL_ACTIVE_BUCKETS,
    ATTR_CAL_CALIBRATED,
    ATTR_CAL_FITTED_AT,
    ATTR_CAL_MAE,
    ATTR_CAL_BAND_SOURCE,
    ATTR_CAL_N_OBS,
    ATTR_CAL_OBS_COUNT,
    ATTR_CAL_P10,
    ATTR_CAL_P50,
    ATTR_CAL_P90,
    ATTR_CAL_SOURCE,
    ATTR_CAL_STATUS,
    ATTR_CAL_SUMMARY,
    ATTR_CAL_TOTAL_BUCKETS,
    ATTR_CHEAPEST_2H,
    ATTR_EXPORTLIMIT,
    ATTR_FORECAST,
    ATTR_FORECAST_GENERATED_AT,
    ATTR_IC_FORECAST,
    ATTR_IMPORTLIMIT,
    ATTR_INTERCONNECTOR_ID,
    ATTR_INTERVAL_MINUTES,
    ATTR_IS_CONSTRAINED,
    ATTR_LAST_CHANGED,
    ATTR_MARGINALVALUE,
    ATTR_MAX_24H,
    ATTR_MAX_VIOLATION_7D,
    ATTR_METEREDMWFLOW,
    ATTR_MIN_24H,
    ATTR_MWFLOW,
    ATTR_MWLOSSES,
    ATTR_NEXT_VALUE,
    ATTR_REGION,
    ATTR_RUN_DATETIME,
    ATTR_SOURCE_FILE,
    ATTR_VIOLATIONDEGREE,
    CONF_ACTIVE_TARIFF,
    CONF_FORECAST_MODE,
    DEFAULT_ENABLED_TARIFFS,
    DEVICE_CONFIGURATION_URL,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DISTRIBUTOR_TARIFFS,
    DOMAIN,
    EXPORT_TARIFF_PROGRAMS,
    FORECAST_MODE_DAYS_2_7,
    FORECAST_MODE_FULL,
    get_region,
    interconnectors_for_regions,
    QLD1_INTERCONNECTORS,
    REGION_DISTRIBUTORS,
    storage_keys,
)
from .calibration_inputs import (
    STPASA_BAND_EDGE_SLACK_H,
    STPASA_COVERAGE_MARGIN_H,
    STPASA_MAX_MATCH_SECONDS,
    STPASA_MAX_HORIZON_H,
    STPASA_MIN_HORIZON_H,
    calibrate_interval,
    calibrated_forecast_key,
    covariates_for_interval,
    horizon_hours,
    interval_key_for_period,
    stpasa_coverage_start,
    stpasa_effective_min_horizon_h,
    stpasa_features_for_interval,
)
from .coordinator import PD7DayCoordinator
from .tariff_sensor import NemPd7dayExportTariffSensor, NemPd7dayTariffSensor, TariffForecastDays27Sensor

if TYPE_CHECKING:
    from datetime import datetime
    from .notice_store import GridNoticeStore

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# The STPASA band, the nearest-match bound, the coverage margin, the horizon
# computation, the per-run band floor and the STPASA feature lookup now live in
# calibration_inputs so the tariff sensors use exactly the same ones. See issue
# #66: the two paths drifted precisely because each had its own copy, and issue
# #68's per-run floor is the same hazard, since a floor that only the forecast
# path applies is a second way for the two sensors to disagree. These names are
# kept as module-level aliases because tests and other modules import them from
# here.
#
# _STPASA_MIN_HORIZON_H is the hard lower bound, not the effective one: it
# encodes the judgement that Amber/CSIRO cover the near term better, which
# holds whatever STPASA happens to cover. The effective lower edge is resolved
# per run by _stpasa_effective_min_horizon_h. Beyond 120h STPASA is
# counterproductive and the pipeline falls through to isotonic-only.
_STPASA_MIN_HORIZON_H = STPASA_MIN_HORIZON_H
_STPASA_MAX_HORIZON_H = STPASA_MAX_HORIZON_H
_STPASA_MAX_MATCH_SECONDS = STPASA_MAX_MATCH_SECONDS
_STPASA_COVERAGE_MARGIN_H = STPASA_COVERAGE_MARGIN_H
_STPASA_BAND_EDGE_SLACK_H = STPASA_BAND_EDGE_SLACK_H
_stpasa_features_for_interval = stpasa_features_for_interval
_stpasa_effective_min_horizon_h = stpasa_effective_min_horizon_h
_stpasa_coverage_start = stpasa_coverage_start
_horizon_hours = horizon_hours

# How many times CalibratedWriteMixin will re-warm the calibrated forecast memo
# when the cache key moves while the warm is in flight. See
# CalibratedWriteMixin._async_warm_until_current for why one attempt is not
# enough and why this is bounded.
_MAX_CALIBRATION_WARM_ATTEMPTS = 3


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PD7DayCoordinator = entry.runtime_data.coordinator
    store = entry.runtime_data.store
    region: str = get_region(entry)

    entities: list[SensorEntity] = []

    entities.append(PD7DayForecastSensor(coordinator, store, entry, region))
    entities.append(PD7DayRegionSourceFileDatetimeSensor(coordinator, entry, region))
    entities.append(PD7DayRegionDataUpdatedDatetimeSensor(coordinator, entry, region))

    # Diagnostic data sensors — expose the full PD7DAY / STPASA datasets as
    # unrecorded attributes (kept out of the HA recorder database).
    entities.append(PD7DayDataSensor(coordinator, store, entry, region))
    entities.append(StpasaDataSensor(coordinator, entry, region))


    # Interconnectors for this region.
    #
    # The entity set comes from the static region map alone, never from the
    # contents of one fetch. Gating creation on live data made the entity
    # population a function of whatever AEMO happened to publish at the moment
    # setup ran, so two restarts of the same install could yield different
    # entities, and an interconnector absent from a single file vanished from
    # dashboards, templates and recorder history with nothing logged. See
    # issue #48. PD7DayInterconnectorSensor.available already reports False
    # when the coordinator holds no row for the id, which is the correct way
    # to surface missing data.
    region_ic_ids = interconnectors_for_regions([region])
    live_ic_ids = set(coordinator.data.interconnectors) if (
        coordinator.data and coordinator.data.interconnectors
    ) else set()
    for ic_id in sorted(region_ic_ids):
        entities.append(PD7DayInterconnectorSensor(coordinator, entry, region, ic_id))
    _LOGGER.debug(
        "PD7DAY %s interconnector entities created=%s, of which without data "
        "in this result=%s",
        region,
        sorted(region_ic_ids),
        sorted(region_ic_ids - live_ic_ids) if live_ic_ids else "no data yet",
    )

    entities.append(PD7DayCalibrationSensor(coordinator, store, entry, region))
    entities.append(PD7DayTodSensor(coordinator, entry, region))

    entities.append(NemPd7dayGridNoticesSensor(coordinator, entry, region, coordinator.notice_store))

    # Tariff forecast sensors — one per (distributor, tariff_code) for this region
    for distributor in REGION_DISTRIBUTORS.get(region, []):
        for tariff_code in DISTRIBUTOR_TARIFFS.get(distributor, []):
            entities.append(
                NemPd7dayTariffSensor(coordinator, entry, region, distributor, tariff_code, store=store)
            )

    # Export tariff sensors — one per export program for this region
    for (dist, import_code), export_code in EXPORT_TARIFF_PROGRAMS.items():
        if dist in REGION_DISTRIBUTORS.get(region, []):
            entities.append(
                NemPd7dayExportTariffSensor(
                    coordinator, entry, region, dist, import_code, export_code, store=store,
                )
            )

    # Day 2-7 additive sensors — only registered in days_2_7 mode
    mode = entry.options.get(CONF_FORECAST_MODE, FORECAST_MODE_DAYS_2_7)
    if mode == FORECAST_MODE_DAYS_2_7:
        entities.append(SpotPriceForecastDays27Sensor(coordinator, store, entry, region))

        # Day 2-7 tariff sensor — only for the active tariff
        active_tariff = entry.options.get(CONF_ACTIVE_TARIFF, "")
        if not active_tariff:
            # Default: first enabled tariff for the region's first distributor
            for dist in REGION_DISTRIBUTORS.get(region, []):
                for code in DISTRIBUTOR_TARIFFS.get(dist, []):
                    if (dist, code) in DEFAULT_ENABLED_TARIFFS:
                        active_tariff = f"{dist}/{code}"
                        break
                if active_tariff:
                    break
        if active_tariff and "/" in active_tariff:
            dist, code = active_tariff.split("/", 1)
            entities.append(
                TariffForecastDays27Sensor(coordinator, entry, region, dist, code, store=store)
            )

    async_add_entities(entities, update_before_add=True)


# ---------------------------------------------------------------------------
# Shared state-write path for the calibration-backed sensors
# ---------------------------------------------------------------------------


class CalibratedWriteMixin:
    """Warms the calibrated forecast off the loop before writing state.

    ``extra_state_attributes`` is a property, so it is evaluated during
    ``async_write_ha_state()``. On a cache miss it runs
    ``_calibrated_forecast()``, which calibrates every interval of the run,
    367 of them at the time of writing. That landed on the event loop inside
    the state write, and Home Assistant reported it:

        Updating state for sensor.nem_pd7day_nsw1_nem_nsw1_pd7day_data
        (PD7DayDataSensor) took 0.493 seconds.

    All five regions fired at once because a new PD7DAY run invalidates every
    region's memo simultaneously, so each region's next state write rebuilt its
    own forecast. Worst single write observed on the live instance was 2.181 s,
    just after a restart.

    The memo added in #40 is correct and unchanged. The problem was that it is
    *lazy*: whichever entity wrote state first paid for the whole rebuild, on
    the loop. This warms it in the executor first, so the write itself only
    ever reads an already-populated cache.

    A cache hit costs one executor round-trip and no calibration, so routing
    every write through here is cheap. It also covers invalidations that do not
    come from a coordinator refresh, such as a calibration refit bumping
    ``fit_generation``, which the previous code could only absorb inside a
    state write.
    """

    def _calibrated_memo(self) -> dict | None:
        """The coordinator's per region memo dict, or None if unusable.

        PD7DayCoordinator initialises this dict. Check the type rather than
        just checking for None, because the tests substitute a MagicMock
        coordinator and attribute access on a mock invents an object instead of
        raising, so getattr alone never reports the attribute as missing.
        """
        cache = getattr(self.coordinator, "_calibrated_forecast_cache", None)
        return cache if isinstance(cache, dict) else None

    def _cached_calibrated_forecast(self, key) -> list[dict] | None:
        """The memoised forecast for ``key``, or None if the memo does not hold it."""
        cache = self._calibrated_memo()
        if cache is None:
            return None
        entry = cache.get(self._region)
        if not (isinstance(entry, tuple) and len(entry) == 2):
            return None
        cached_key, cached_val = entry
        if cached_key != key or cached_val is None:
            return None
        return cached_val

    async def _async_warm_calibrated_forecast(self) -> None:
        """Populate the coordinator memo for this region, off the event loop.

        The key is taken ONCE here, on the loop, before the executor hop, and
        then carried through to the publish. It used to be taken inside the
        executor job by ``_calibrated_forecast`` itself, which meant the warm
        did not know which key it had stored under and could not tell whether
        the world had moved while it was away. Two things went wrong with that:

          * A pass that started before a refit published its result under the
            pre refit key, unconditionally. The memo has a single slot per
            region shared by three entity classes, so a warm that started early
            and landed late overwrote the current entry a sibling entity had
            just published, and the next reader of that slot paid for a full
            rebuild on the loop.
          * ``_calibrate_period`` reads the calibration store live, so a
            generation change part way through a pass produced a list built
            from two different models, stored under the key of the first.

        So: take the key, compute the values with no cache access at all, then
        publish only if the key is still the one the write will ask for. There
        is no await between the recheck and the publish, and everything the key
        folds in is mutated only from the loop, so the recheck cannot go stale
        between the two. If the key did move we publish nothing and leave the
        slot alone; ``_async_warm_until_current`` will come round again with
        the new key.
        """
        d = self._price_data
        if d is None:
            return
        key = self._calibrated_forecast_key(d)
        if self._cached_calibrated_forecast(key) is not None:
            # Already warm for this key. Skipping the executor hop here is why
            # a hit costs nothing, which matters because every dispatch tick
            # routes five minute writes through this path.
            return
        try:
            value = await self.hass.async_add_executor_job(
                self._calibrated_forecast_values, d
            )
        except Exception:  # noqa: BLE001 - warming is best effort
            # The lazy path inside extra_state_attributes remains as the
            # correctness fallback, so a failed warm costs speed, not data.
            _LOGGER.debug(
                "Calibrated forecast warm failed for %s, falling back to the "
                "lazy path",
                getattr(self, "entity_id", None),
                exc_info=True,
            )
            return
        if self._price_data is not d or self._calibrated_forecast_key(d) != key:
            # Superseded while we were in the executor. Publishing now would
            # label a stale list with a key that no longer describes it, and
            # could overwrite a fresher entry from a sibling entity.
            _LOGGER.debug(
                "Calibrated forecast key moved during the warm for %s, "
                "discarding the result rather than publishing it",
                getattr(self, "entity_id", None),
            )
            return
        cache = self._calibrated_memo()
        if cache is not None:
            cache[self._region] = (key, value)

    def _calibrated_cache_is_current(self) -> bool:
        """Whether the memo already holds the value this entity's write will ask for.

        Deliberately builds the key the same way ``_calibrated_forecast`` does,
        refreshing the coordinator STPASA index, so the answer is exactly what
        the property will compute rather than an approximation of it.

        This is only sound because every caller invokes it with no ``await``
        between the check and ``async_write_ha_state()``. ``_stpasa_index_run``
        and ``fit_generation`` are only ever mutated from coroutines and loop
        callbacks, so within a single loop iteration nothing can move them
        underneath us. Introducing an await between the two would reopen the
        race this exists to close.
        """
        d = self._price_data
        if d is None:
            # Nothing to calibrate, so the write cannot pay for a rebuild.
            return True
        return self._cached_calibrated_forecast(
            self._calibrated_forecast_key(d)
        ) is not None

    async def _async_warm_until_current(self) -> None:
        """Warm the memo, re-warming while the key keeps moving underneath us.

        A single warm is not enough. The memo key folds in the STPASA index key
        (``run_datetime|fetched_at``) and ``CalibrationStore.fit_generation``,
        and both can change without a coordinator refresh: any STPASA refetch
        moves ``fetched_at``, and ``async_refit`` plus the OLS stage 2 attach
        each bump ``fit_generation``. The warm itself takes roughly 0.4 s in the
        executor, which is ample time for a refit triggered by the same new run
        to land. When that happened the write missed the memo and recalibrated
        all 357 intervals on the event loop, which is the whole thing #58 set
        out to prevent. Measured live on v3.3.1 at a 30 minute boundary:

            Updating state for sensor.nem_pd7day_tas1_price_forecast
            (PD7DayForecastSensor) took 6.766 seconds.

        Bounded rather than unbounded: if the inputs are churning faster than a
        warm completes, three attempts is already an unusual amount of executor
        work to spend on one state write, and falling through to the lazy path
        costs speed, not correctness.

        The retry is the outer half of the guarantee. The inner half is in
        ``_async_warm_calibrated_forecast``, which refuses to publish a result
        computed under a key that has since moved. Without that, a retry could
        still leave the slot holding the superseded list it had just written.
        """
        for attempt in range(1, _MAX_CALIBRATION_WARM_ATTEMPTS + 1):
            await self._async_warm_calibrated_forecast()
            if self._calibrated_cache_is_current():
                return
            _LOGGER.debug(
                "Calibrated forecast key moved during warm attempt %s/%s for %s",
                attempt,
                _MAX_CALIBRATION_WARM_ATTEMPTS,
                getattr(self, "entity_id", None),
            )

    async def async_added_to_hass(self) -> None:
        """Warm before the platform's own first state write.

        ``async_add_entities(..., update_before_add=True)`` does not route the
        first write through ``_handle_coordinator_update``. Home Assistant's
        ``Entity.add_to_platform_finish`` is:

            await self.async_internal_added_to_hass()
            await self.async_added_to_hass()
            self.async_write_ha_state()

        so that write is a direct call from the add flow and the mixin never saw
        it. Every entity therefore paid for a full recalibration on the loop
        exactly once, during setup, which is precisely the restart condition
        #55 was reported from. This hook is the last thing awaited before that
        write, and there is no await between it and the write, so warming here
        covers it.

        The memo is shared per region across the three sensors that use it, so
        only the first entity of a region does real work.
        """
        await super().async_added_to_hass()
        await self._async_warm_until_current()

    async def _async_warm_then_write(self) -> None:
        await self._async_warm_until_current()
        self.async_write_ha_state()

    def _schedule_warm_state_write(self) -> None:
        """Warm the memo then write state, without blocking the caller."""
        self.hass.async_create_task(self._async_warm_then_write())

    def _handle_coordinator_update(self) -> None:
        """Replaces CoordinatorEntity's direct async_write_ha_state()."""
        self._schedule_warm_state_write()


# ---------------------------------------------------------------------------
# Price forecast sensor — with calibration
# ---------------------------------------------------------------------------

class PD7DayForecastSensor(
    CalibratedWriteMixin, CoordinatorEntity[PD7DayCoordinator], SensorEntity
):
    """
    Regional spot price forecast.

    State: calibrated $/kWh when calibration is active, raw PD7DAY otherwise.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = None
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTR_ATTRIBUTION
    _unrecorded_attributes = frozenset({"forecast", "forecast_description"})

    def __init__(self, coordinator, store, entry: ConfigEntry, region: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        self._store = store
        slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{slug}_forecast"
        self._attr_name = "NEM Spot Price Forecast"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    # Run-keyed calibrated-forecast cache. The calibrated forecast only changes
    # when a new PD7DAY run lands or STPASA/calibration is refit, so we memoise
    # the per-interval calibration (the expensive path) and reuse it across
    # state writes that carry an unchanged run.
    #
    # The cache lives on the coordinator, not on the entity. The coordinator is
    # per region and so is the calibrated forecast: PD7DayForecastSensor,
    # SpotPriceForecastDays27Sensor and PD7DayDataSensor all call this for the
    # same region with the same coordinator and the same calibration store, and
    # their _calibrate_period and _covariates_for_interval implementations are
    # byte for byte the same computation. Memoising on the entity therefore ran
    # the full recalibration of roughly 336 intervals three times per region
    # instead of once, which is what made platform setup take 42 to 53 seconds
    # per region.
    def _calibrated_forecast_key(self, d) -> tuple:
        """Build the memo key for PriceData ``d``.

        Split out of ``_calibrated_forecast`` so ``CalibratedWriteMixin`` can ask
        whether the memo is current without recomputing the forecast to find
        out. Both callers must derive the key identically or the check is
        worthless, so there is deliberately only one implementation.

        The body moved to ``calibration_inputs.calibrated_forecast_key`` when
        the tariff sensors started reading the same memo (issue #62). A reader
        that decided currency by its own rule while the writer stored under
        another would publish a stale price rather than fail, which is the
        issue #66 failure mode again.
        """
        return calibrated_forecast_key(self.coordinator, self._store, self._region, d)

    def _calibrated_forecast(self, d) -> list[dict]:
        """
        Return the calibrated forecast list for PriceData ``d``, memoised per
        region on the PD7DAY run plus the STPASA index and calibration versions.

        Recomputes only when an input that affects calibration changes:
          * ``forecast_generated_at`` and interval count (new PD7DAY run),
          * the STPASA index cache key (new/refetched STPASA run),
          * the calibration store's fit generation (refit or OLS stage 2).
        Otherwise the previously computed list is returned unchanged, avoiding
        the full per-interval recalibration on every state write.

        This is the lazy fallback path and it runs on the event loop, so the
        key cannot move between the read and the write here: there is no await
        anywhere in it. The warm path in ``CalibratedWriteMixin`` does have an
        await in the middle and has to guard the publish itself.
        """
        key = self._calibrated_forecast_key(d)
        cache = self._calibrated_memo()
        if cache is None:
            cache = {}
            try:
                self.coordinator._calibrated_forecast_cache = cache
            except (AttributeError, TypeError):  # pragma: no cover - read-only mock
                pass
        cached = self._cached_calibrated_forecast(key)
        if cached is not None:
            return cached
        value = self._calibrated_forecast_values(d)
        cache[self._region] = (key, value)
        return value

    def _calibrated_forecast_values(self, d) -> list[dict]:
        """Calibrate every interval of ``d``. No cache read, no cache write.

        Kept free of memo access on purpose, because this is the half that runs
        in the executor. Whether the result is fit to publish depends on state
        that only the event loop may read consistently, so that decision is
        made by the caller once it is back on the loop.
        """
        run_at = d.forecast_generated_at
        return [self._calibrate_period(p, run_at) for p in d.forecast]

    @property
    def _price_data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.prices.get(self._region)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._price_data is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to 30-min interval ticks and DispatchCoordinator for 5-min updates."""
        await super().async_added_to_hass()

        async def _handle_tick(_now) -> None:
            self._schedule_warm_state_write()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                _handle_tick,
                minute=[0, 30],
                second=5,
            )
        )

        # Subscribe to DispatchCoordinator so state refreshes every 5 minutes
        runtime_data = getattr(self._entry, "runtime_data", None)
        dispatch_coordinator = runtime_data.dispatch if runtime_data else None
        if dispatch_coordinator is not None:
            self.async_on_remove(
                dispatch_coordinator.async_add_listener(
                    self._schedule_warm_state_write
                )
            )

    def _current_period(self, forecast: list):
        """Return the forecast period whose interval covers the current NEM time."""
        now = now_nem()
        for period in forecast:
            try:
                interval_start = parse_iso(period.time)
                interval_end = parse_iso(period.nemtime)
                if interval_start <= now < interval_end:
                    return period
            except (ValueError, TypeError):
                continue
        # Fallback: first period (covers startup before first interval boundary)
        return forecast[0] if forecast else None

    def _covariates_for_interval(self, interval_key: str) -> dict:
        """Extract gas_forecast_tj and qni_mwflow for an interval from coordinator data.

        Kept as a method because subclasses and tests reach for it by name; the
        body lives in calibration_inputs so the tariff sensors use the same one.
        """
        return covariates_for_interval(self.coordinator, interval_key)

    @property
    def native_value(self) -> float | None:
        # Try 5-minute dispatch price first
        runtime_data = getattr(self._entry, "runtime_data", None)
        dispatch = runtime_data.dispatch if runtime_data else None
        if dispatch and dispatch.prices.get(self._region):
            return dispatch.prices[self._region].rrp

        # Fallback: current interval from PD7DAY
        d = self._price_data
        if d is None:
            return None
        period = self._current_period(d.forecast)
        if period is None:
            return None
        if self._store:
            h = _horizon_hours(d.forecast_generated_at, period.time)
            try:
                hour = parse_iso(period.time).hour
            except (ValueError, TypeError):
                hour = now_nem().hour
            interval_key = interval_key_for_period(period)
            # Same shared call as the forecast attribute path, so the published
            # state and the forecast entry for the current interval cannot
            # disagree. See issue #66. run_at carries through for the per-run
            # stage-2 band floor of issue #68.
            cal = calibrate_interval(
                self._store, self.coordinator, period.value, interval_key, h, hour,
                run_at_iso=d.forecast_generated_at,
            )
            if cal is None:
                return None
            return cal["calibrated"]
        return period.value

    def _calibrate_period(self, period, run_at_str: str | None) -> dict:
        """Build the enriched forecast dict for one PricePeriod."""
        h = _horizon_hours(run_at_str, period.time)
        try:
            hour = parse_iso(period.time).hour
        except (ValueError, TypeError):
            hour = 0

        interval_key = interval_key_for_period(period)

        base = {
            "nemtime": to_nem_iso(parse_iso(period.nemtime)),
            "time": interval_key,
            "raw_value": period.value,
            "horizon_hours": round(h, 1),
        }

        # run_at_str is passed on so the stage-2 band floor is resolved from
        # this run's STPASA coverage (issue #68) rather than the static
        # constant. It is threaded through the shared entry point, not applied
        # here, so the tariff path gets the same floor.
        cal = calibrate_interval(
            self._store, self.coordinator, period.value, interval_key, h, hour,
            run_at_iso=run_at_str,
        )
        if cal is not None:
            cal_update = {
                ATTR_CAL_CALIBRATED: cal["calibrated"],
                ATTR_CAL_P10: cal["p10"],
                ATTR_CAL_P50: cal["p50"],
                ATTR_CAL_P90: cal["p90"],
                ATTR_CAL_MAE: cal.get("ols_mae"),
                ATTR_CAL_SOURCE: cal["calibrated_source"],
                ATTR_CAL_BAND_SOURCE: cal.get(ATTR_CAL_BAND_SOURCE),
                ATTR_CAL_N_OBS: cal["n_obs"],
                "value": cal["calibrated"],
                "spike_credible": cal.get("spike_credible"),
            }
            if cal.get("stpasa_run_at"):
                cal_update["stpasa_run_at"] = cal["stpasa_run_at"]
            base.update(cal_update)
        else:
            # No calibration store: raw passthrough, as before. Store present
            # but no raw price: there is no honest calibrated number, so the
            # entry carries the raw value through and never a stand-in 0.
            base["value"] = period.value

        return base

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._price_data
        if d is None:
            return {}

        run_at = d.forecast_generated_at
        calibrated_forecast = self._calibrated_forecast(d)

        # Base sensor always provides full day 1-7 forecast
        trimmed_forecast = calibrated_forecast

        # Min/max over trimmed window (use calibrated 'value' field)
        trimmed_values = [
            p.get("value") for p in trimmed_forecast if p.get("value") is not None
        ]
        min_value = round(min(trimmed_values), 6) if trimmed_values else None
        max_value = round(max(trimmed_values), 6) if trimmed_values else None

        # Cheapest 2h window over trimmed forecast
        # Find the 4 consecutive intervals (30-min each = 2h) with lowest average 'value'
        n = 4
        cheapest_window = None
        if len(trimmed_forecast) >= n:
            for i in range(len(trimmed_forecast) - n + 1):
                window = trimmed_forecast[i : i + n]
                vals = [p.get("value") for p in window if p.get("value") is not None]
                if len(vals) == n:
                    avg = round(sum(vals) / n, 6)
                    if cheapest_window is None or avg < cheapest_window["avg_value"]:
                        cheapest_window = {
                            "nemtime_start": window[0].get("nemtime"),
                            "nemtime_end": window[-1].get("nemtime"),
                            "start": window[0].get("time"),
                            "end": window[-1].get("time"),
                            "avg_value": avg,
                            "points": n,
                        }

        return {
            ATTR_REGION: d.region,
            ATTR_FORECAST_GENERATED_AT: run_at,
            ATTR_INTERVAL_MINUTES: d.interval_minutes,
            ATTR_NEXT_VALUE: (
                trimmed_forecast[0].get(ATTR_CAL_CALIBRATED, trimmed_forecast[0].get("value"))
                if trimmed_forecast
                else None
            ),
            ATTR_MIN_24H: min_value,
            ATTR_MAX_24H: max_value,
            ATTR_CHEAPEST_2H: cheapest_window,
            ATTR_FORECAST: trimmed_forecast,
            ATTR_SOURCE_FILE: d.source_file,
            "calibration_active": (
                self._store is not None
                and self._store.calibration is not None
                and self._store.active_bucket_count > 0
            ),
        }



class SpotPriceForecastDays27Sensor(
    CalibratedWriteMixin, CoordinatorEntity[PD7DayCoordinator], SensorEntity
):
    """
    Day 2-7 spot price forecast sensor.

    Only registered when forecast_mode == FORECAST_MODE_DAYS_2_7.
    State: same as base sensor (current dispatch/PD7DAY price).
    Forecast attribute: trimmed to post-amber-express-cutoff intervals only.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = None
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_icon = "mdi:transmission-tower"
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTR_ATTRIBUTION
    _attr_entity_registry_enabled_default = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"forecast", "forecast_description"})

    def __init__(self, coordinator, store, entry: ConfigEntry, region: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        self._store = store
        slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{slug}_forecast_days27"
        self._attr_name = "Day 2-7 NEM Spot Price Forecast"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _price_data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.prices.get(self._region)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._price_data is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_tick(_now) -> None:
            self._schedule_warm_state_write()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                _handle_tick,
                minute=[0, 30],
                second=5,
            )
        )

        # Subscribe to DispatchCoordinator so state refreshes every 5 minutes
        runtime_data = getattr(self._entry, "runtime_data", None)
        dispatch_coordinator = runtime_data.dispatch if runtime_data else None
        if dispatch_coordinator is not None:
            self.async_on_remove(
                dispatch_coordinator.async_add_listener(
                    self._schedule_warm_state_write
                )
            )

    def _current_period(self, forecast: list):
        now = now_nem()
        for period in forecast:
            try:
                interval_start = parse_iso(period.time)
                interval_end = parse_iso(period.nemtime)
                if interval_start <= now < interval_end:
                    return period
            except (ValueError, TypeError):
                continue
        return forecast[0] if forecast else None

    def _covariates_for_interval(self, interval_key: str) -> dict:
        """Same computation as PD7DayForecastSensor, delegated to one body."""
        return covariates_for_interval(self.coordinator, interval_key)

    @property
    def native_value(self) -> float | None:
        # Same as base sensor: try dispatch first, then PD7DAY
        runtime_data = getattr(self._entry, "runtime_data", None)
        dispatch = runtime_data.dispatch if runtime_data else None
        if dispatch and dispatch.prices.get(self._region):
            return dispatch.prices[self._region].rrp
        d = self._price_data
        if d is None:
            return None
        period = self._current_period(d.forecast)
        if period is None:
            return None
        if self._store:
            h = _horizon_hours(d.forecast_generated_at, period.time)
            try:
                hour = parse_iso(period.time).hour
            except (ValueError, TypeError):
                hour = now_nem().hour
            interval_key = interval_key_for_period(period)
            cal = calibrate_interval(
                self._store, self.coordinator, period.value, interval_key, h, hour,
                run_at_iso=d.forecast_generated_at,
            )
            if cal is None:
                return None
            return cal["calibrated"]
        return period.value

    def _calibrate_period(self, period, run_at_str: str | None) -> dict:
        h = _horizon_hours(run_at_str, period.time)
        try:
            hour = parse_iso(period.time).hour
        except (ValueError, TypeError):
            hour = 0
        interval_key = interval_key_for_period(period)
        base = {
            "nemtime": to_nem_iso(parse_iso(period.nemtime)),
            "time": interval_key,
            "raw_value": period.value,
            "horizon_hours": round(h, 1),
        }
        # run_at_str is passed on so the stage-2 band floor is resolved from
        # this run's STPASA coverage (issue #68) rather than the static
        # constant. It is threaded through the shared entry point, not applied
        # here, so the tariff path gets the same floor.
        cal = calibrate_interval(
            self._store, self.coordinator, period.value, interval_key, h, hour,
            run_at_iso=run_at_str,
        )
        if cal is not None:
            cal_update = {
                ATTR_CAL_CALIBRATED: cal["calibrated"],
                ATTR_CAL_P10: cal["p10"],
                ATTR_CAL_P50: cal["p50"],
                ATTR_CAL_P90: cal["p90"],
                ATTR_CAL_MAE: cal.get("ols_mae"),
                ATTR_CAL_SOURCE: cal["calibrated_source"],
                ATTR_CAL_BAND_SOURCE: cal.get(ATTR_CAL_BAND_SOURCE),
                ATTR_CAL_N_OBS: cal["n_obs"],
                "value": cal["calibrated"],
                "spike_credible": cal.get("spike_credible"),
            }
            if cal.get("stpasa_run_at"):
                cal_update["stpasa_run_at"] = cal["stpasa_run_at"]
            base.update(cal_update)
        else:
            # No calibration store: raw passthrough, as before. Store present
            # but no raw price: there is no honest calibrated number, so the
            # entry carries the raw value through and never a stand-in 0.
            base["value"] = period.value
        return base

    # Run-keyed calibrated-forecast cache (shared implementation). Memoises the
    # per-interval calibration so unchanged runs are not recomputed on every
    # state write. The key builder must come with it, or CalibratedWriteMixin
    # would check currency against a different key than the memo stored.
    _calibrated_forecast_key = PD7DayForecastSensor._calibrated_forecast_key
    _calibrated_forecast_values = PD7DayForecastSensor._calibrated_forecast_values
    _calibrated_forecast = PD7DayForecastSensor._calibrated_forecast

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._price_data
        if d is None:
            return {}
        run_at = d.forecast_generated_at
        calibrated_forecast = self._calibrated_forecast(d)
        # Day 2-7: trim to post-amber-express-cutoff only
        cutoff_dt = _amber_express_cutoff()
        trimmed_forecast = [
            p for p in calibrated_forecast
            if parse_iso(p["time"]) > cutoff_dt
        ]
        trimmed_values = [
            p.get("value") for p in trimmed_forecast if p.get("value") is not None
        ]
        min_value = round(min(trimmed_values), 6) if trimmed_values else None
        max_value = round(max(trimmed_values), 6) if trimmed_values else None
        n = 4
        cheapest_window = None
        if len(trimmed_forecast) >= n:
            for i in range(len(trimmed_forecast) - n + 1):
                window = trimmed_forecast[i : i + n]
                vals = [p.get("value") for p in window if p.get("value") is not None]
                if len(vals) == n:
                    avg = round(sum(vals) / n, 6)
                    if cheapest_window is None or avg < cheapest_window["avg_value"]:
                        cheapest_window = {
                            "nemtime_start": window[0].get("nemtime"),
                            "nemtime_end": window[-1].get("nemtime"),
                            "start": window[0].get("time"),
                            "end": window[-1].get("time"),
                            "avg_value": avg,
                            "points": n,
                        }
        return {
            ATTR_REGION: d.region,
            ATTR_FORECAST_GENERATED_AT: run_at,
            ATTR_INTERVAL_MINUTES: d.interval_minutes,
            ATTR_NEXT_VALUE: (
                trimmed_forecast[0].get(ATTR_CAL_CALIBRATED, trimmed_forecast[0].get("value"))
                if trimmed_forecast
                else None
            ),
            ATTR_MIN_24H: min_value,
            ATTR_MAX_24H: max_value,
            ATTR_CHEAPEST_2H: cheapest_window,
            ATTR_FORECAST: trimmed_forecast,
            ATTR_SOURCE_FILE: d.source_file,
            "calibration_active": (
                self._store is not None
                and self._store.calibration is not None
                and self._store.active_bucket_count > 0
            ),
        }


class PD7DayRegionSourceFileDatetimeSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Per-region diagnostic timestamp for latest source file run datetime."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: PD7DayCoordinator, entry: ConfigEntry, region: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_source_file_datetime"
        self._attr_name = "Source File Datetime"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _price_data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.prices.get(self._region)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._price_data is not None

    @property
    def native_value(self):
        d = self._price_data
        if d is None or not d.forecast_generated_at:
            return None
        return parse_iso(d.forecast_generated_at)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._price_data
        if d is None:
            return {}
        return {
            ATTR_REGION: self._region,
            ATTR_SOURCE_FILE: d.source_file,
        }


class PD7DayRegionDataUpdatedDatetimeSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Per-region diagnostic timestamp for latest coordinator data refresh."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: PD7DayCoordinator, entry: ConfigEntry, region: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_data_updated_datetime"
        self._attr_name = "Data Updated"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        updated_at = getattr(self.coordinator.data, "updated_at", None)
        if not updated_at:
            return None
        return parse_iso(updated_at)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_REGION: self._region,
            ATTR_SOURCE_FILE: self.coordinator.data.source_file if self.coordinator.data else None,
        }


# ---------------------------------------------------------------------------
# Interconnector sensor
# ---------------------------------------------------------------------------

class PD7DayInterconnectorSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Interconnector MW flow and constraint forecast."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "MW"
    _attr_device_class = None
    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
        ic_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        self._ic_id = ic_id
        ic_slug = ic_id.lower().replace("-", "_")
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_ic_{ic_slug}"
        self._attr_name = f"Interconnector {ic_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.interconnectors.get(self._ic_id)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._data is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to 30-min interval ticks so state updates without a new fetch."""
        await super().async_added_to_hass()

        async def _handle_tick(_now) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                _handle_tick,
                minute=[0, 30],
                second=5,
            )
        )

    def _current_ic_period(self, forecast: list):
        """Return the interconnector period covering the current NEM time."""
        now = now_nem()
        for period in forecast:
            try:
                interval_start = parse_iso(period.time)
                interval_end = parse_iso(period.nemtime)
                if interval_start <= now < interval_end:
                    return period
            except (ValueError, TypeError):
                continue
        return forecast[0] if forecast else None

    @property
    def native_value(self) -> float | None:
        d = self._data
        if d is None:
            return None
        period = self._current_ic_period(d.forecast)
        return period.mwflow if period else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._data
        if d is None:
            return {}
        current = self._current_ic_period(d.forecast)
        return {
            ATTR_INTERCONNECTOR_ID: d.interconnector_id,
            ATTR_RUN_DATETIME: d.run_datetime,
            ATTR_SOURCE_FILE: d.source_file,
            ATTR_IS_CONSTRAINED: d.is_constrained,
            ATTR_VIOLATIONDEGREE: current.violationdegree if current else None,
            ATTR_MAX_VIOLATION_7D: d.max_violation_7d,
            ATTR_MWFLOW: current.mwflow if current else None,
            ATTR_METEREDMWFLOW: current.meteredmwflow if current else None,
            ATTR_MWLOSSES: current.mwlosses if current else None,
            ATTR_MARGINALVALUE: current.marginalvalue if current else None,
            ATTR_EXPORTLIMIT: current.exportlimit if current else None,
            ATTR_IMPORTLIMIT: current.importlimit if current else None,
            ATTR_IC_FORECAST: [
                {
                    "nemtime": to_nem_iso(parse_iso(p.nemtime)),
                    "time": to_nem_iso(parse_iso(p.time)),
                    "mwflow": p.mwflow,
                    "violationdegree": p.violationdegree,
                    "marginalvalue": p.marginalvalue,
                    "exportlimit": p.exportlimit,
                    "importlimit": p.importlimit,
                }
                for p in d.forecast
            ],
        }


# ---------------------------------------------------------------------------
# Calibration diagnostic sensor
# ---------------------------------------------------------------------------

class PD7DayCalibrationSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Calibration pipeline status sensor."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        store,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        self._store = store
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_calibration"
        self._attr_name = "Calibration"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> int:
        return self._store.observation_count if self._store else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._store:
            return {ATTR_CAL_STATUS: "store_unavailable"}
        attrs = self._store.summary_attributes()
        attrs[ATTR_REGION] = self._region
        # Merge forecast history metadata
        fh = self._store._forecast_history if self._store else {}
        if fh:
            attrs["forecast_history_entries"] = int(
                sum(len(v) for v in fh.values())
            )
            attrs["forecast_history_intervals"] = len(fh)
            attrs["forecast_history_oldest"] = min(fh.keys())
            attrs["forecast_history_newest"] = max(fh.keys())
            attrs["forecast_history_runs_avg"] = round(
                sum(len(v) for v in fh.values()) / len(fh), 1
            )
        else:
            attrs["forecast_history_entries"] = 0
            attrs["forecast_history_intervals"] = 0
            attrs["forecast_history_oldest"] = None
            attrs["forecast_history_newest"] = None
            attrs["forecast_history_runs_avg"] = 0
        return attrs


# ---------------------------------------------------------------------------
# Time-of-day actual price statistics sensor
# ---------------------------------------------------------------------------

class PD7DayTodSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """
    Reports the mean actual price for the current 30-minute time-of-day slot
    ($/kWh), with full per-slot statistics as attributes.

    State:   mean actual $/kWh for the current slot (or None before enough data)
    Attrs:   unique_intervals, date_from, date_to, slots (list of 48 dicts)

    Updates every 30 minutes alongside the forecast sensors via
    async_track_time_change, and on every coordinator refresh.
    """

    _attr_has_entity_name = True
    _attr_name = "Price ToD Stats"
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:clock-time-four-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"slots"})

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        super().__init__(coordinator)
        self._region = region
        self._entry = entry
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_tod_stats"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
        )

    @property
    def native_value(self) -> float | None:
        tod = getattr(self.coordinator, "tod_stats", None)
        if tod is None:
            return None
        slot = tod.slot_for_now(now_nem())
        if slot is None or slot.n == 0:
            return None
        return round(slot.mean, 6)

    @property
    def extra_state_attributes(self) -> dict:
        tod = getattr(self.coordinator, "tod_stats", None)
        if tod is None:
            return {}
        return tod.as_attributes()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_tick(_now) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                _handle_tick,
                minute=[0, 30],
                second=5,
            )
        )


# ---------------------------------------------------------------------------
# Grid Notices sensor — active MSL/LOR notice count
# ---------------------------------------------------------------------------

class NemPd7dayGridNoticesSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """
    Sensor reporting count of active MSL/LOR market notices for the region.

    State: integer count of active (non-cancelled) notices within next 7 days.

    The ``notices`` attribute is excluded from the recorder (unbounded list).
    Attributes: structured notice list + summary counts by type/level.
    """

    _attr_has_entity_name = True
    _attr_name = "Grid Notices"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "notices"
    _unrecorded_attributes = frozenset({"notices"})

    def __init__(
        self,
        coordinator: "PD7DayCoordinator",
        entry: ConfigEntry,
        region: str,
        notice_store: "GridNoticeStore",
    ) -> None:
        super().__init__(coordinator)
        self._region = region
        self._notice_store = notice_store
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_grid_notices"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{region}")},
            name=f"NEM PD7DAY {region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Sensor is available only when the notice store is initialised."""
        return self._notice_store is not None and self.coordinator.last_update_success

    @property
    def native_value(self) -> int:
        """Count of active non-cancelled notices within next 7 days."""
        if self._notice_store is None:
            return 0
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=10)))
        horizon = now + timedelta(days=7)
        return len(self._notice_store.get_active_notices(
            self._region, from_dt=now, to_dt=horizon
        ))

    @property
    def extra_state_attributes(self) -> dict:
        if self._notice_store is None:
            return {"region": self._region}
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=10)))
        horizon = now + timedelta(days=7)
        active = self._notice_store.get_active_notices(
            self._region, from_dt=now, to_dt=horizon
        )
        lor_notices = [n for n in active if n.notice_type == "LOR"]
        msl_notices = [n for n in active if n.notice_type == "MSL"]
        max_lor = max((n.level for n in lor_notices), default=None)
        max_msl = max((n.level for n in msl_notices), default=None)
        next_from = min((n.period_from for n in active), default=None)

        return {
            "region": self._region,
            "active_count": len(active),
            "lor_active": len(lor_notices),
            "msl_active": len(msl_notices),
            "max_lor_level": max_lor,
            "max_msl_level": max_msl,
            "next_notice_from": next_from.isoformat() if next_from else None,
            "notices": [n.to_dict() for n in active],
            "last_fetched": self._notice_store.last_fetched_at.isoformat()
                if hasattr(self._notice_store, "last_fetched_at") and self._notice_store.last_fetched_at
                else None,
        }


# ---------------------------------------------------------------------------
# Diagnostic data sensors — full datasets as unrecorded attributes
# ---------------------------------------------------------------------------

class PD7DayDataSensor(
    CalibratedWriteMixin, CoordinatorEntity[PD7DayCoordinator], SensorEntity
):
    """Diagnostic sensor exposing the full PD7DAY forecast as an attribute.

    State is the PD7DAY run datetime (forecast_generated_at).  The full
    forecast list is exposed under the unrecorded ``forecast`` attribute so the
    HA recorder never persists the large payload.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        store,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        super().__init__(coordinator)
        self._store = store
        self._entry = entry
        self._region = region
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_pd7day_data"
        self._attr_name = f"NEM {region} PD7DAY Data"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _price_data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.prices.get(self._region)

    @property
    def native_value(self):
        d = self._price_data
        if d is None or not d.forecast_generated_at:
            return STATE_UNAVAILABLE
        return d.forecast_generated_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._price_data
        if d is None:
            return {}

        run_at = d.forecast_generated_at
        forecast = [
            {
                "time": cal.get("time"),
                "nemtime": cal.get("nemtime"),
                "raw_rrp": cal.get("raw_value"),
                "calibrated": cal.get(ATTR_CAL_CALIBRATED),
                "p10": cal.get(ATTR_CAL_P10),
                "p90": cal.get(ATTR_CAL_P90),
                "calibrated_source": cal.get(ATTR_CAL_SOURCE),
                "horizon_hours": cal.get("horizon_hours"),
            }
            for cal in self._calibrated_forecast(d)
        ]

        return {
            ATTR_RUN_DATETIME: run_at,
            ATTR_REGION: self._region,
            "interval_count": len(forecast),
            ATTR_FORECAST: forecast,
        }

    # Reuse the calibration logic from PD7DayForecastSensor for a single period.
    _covariates_for_interval = PD7DayForecastSensor._covariates_for_interval
    _calibrate_period = PD7DayForecastSensor._calibrate_period
    _calibrated_forecast_key = PD7DayForecastSensor._calibrated_forecast_key
    _calibrated_forecast_values = PD7DayForecastSensor._calibrated_forecast_values
    _calibrated_forecast = PD7DayForecastSensor._calibrated_forecast


class StpasaDataSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Diagnostic sensor exposing the full STPASA intervals as an attribute.

    State is the STPASA run datetime.  The STPASA dataset is read live from the
    per-region store at attribute-build time (the PD7DAY coordinator only drives
    refresh cadence).  The intervals list is exposed under the unrecorded
    ``intervals`` attribute so the HA recorder never persists the large payload.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"intervals"})

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_stpasa_data"
        self._attr_name = f"NEM {region} STPASA Data"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    def _latest(self):
        store = self.hass.data.get(DOMAIN, {}).get("stpasa_stores", {}).get(self._region)
        if store is None:
            return None
        return store.latest()

    @property
    def native_value(self):
        result = self._latest()
        if result is None or not result.run_datetime:
            return STATE_UNAVAILABLE
        return result.run_datetime

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._latest()
        if result is None:
            return {}

        intervals = [
            {
                "interval_datetime": si.interval_datetime,
                "demand10": si.demand10,
                "demand50": si.demand50,
                "demand90": si.demand90,
                "surpluscapacity": si.surpluscapacity,
                "ss_solar_uigf": si.ss_solar_uigf,
                "ss_wind_uigf": si.ss_wind_uigf,
            }
            for si in result.intervals
        ]

        # Surface where coverage actually begins and the stage-2 band edge it
        # resolves to, so the uncovered window is visible in diagnostics rather
        # than having to be inferred by comparing sensor attributes by hand.
        # coverage_start is None, not a placeholder, when a run carries no
        # parseable interval. ols_band_min_horizon_h always reports the edge
        # the serving path will actually apply, which is the static constant
        # whenever coverage or run_at is unknown.
        coverage_start_iso, coverage_start_epoch = _stpasa_coverage_start(result)
        price_data = self.coordinator.data
        run_at_iso = (
            getattr(price_data, "forecast_generated_at", None)
            if price_data is not None
            else None
        )

        return {
            ATTR_RUN_DATETIME: result.run_datetime,
            ATTR_REGION: self._region,
            "interval_count": len(intervals),
            "coverage_start": coverage_start_iso,
            "ols_band_min_horizon_h": round(
                _stpasa_effective_min_horizon_h(run_at_iso, coverage_start_epoch), 2
            ),
            "ols_band_max_horizon_h": _STPASA_MAX_HORIZON_H,
            "intervals": intervals,
        }
