"""One region's calibrated forecast memo, and the rules for sharing it.

Every sensor that publishes a calibrated price for an interval of a PD7DAY run
reads one per region memo of the calibrated forecast. ``CalibratedForecast``
owns the protocol around it: the key, reading and publishing the memo slot, the
lazy build, the currency check, the guarded publish after an off-loop build,
and the tariff sensors' calibrated spot view of the same memo. Spec 003.

It is a stateless view built from ``(coordinator, store, region)`` on each
access. The memo slots themselves stay on the coordinator attributes named in
``calibration_inputs`` (``_calibrated_forecast_cache`` and
``_calibrated_spot_cache``), which the tests read and replace directly. The
entities keep what is Home Assistant's: the executor hop, background tasks,
cancellation on removal and the state write.

This module must not import Home Assistant, ``sensor`` or ``tariff_sensor``.
It calls the ``calibration_inputs`` helpers rather than reimplementing them,
because one implementation of each is the rule (#66): a reader that decided
currency by its own rule while the writer stored under another would publish a
stale price rather than fail.
"""
from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from typing import Any

from .calibration_inputs import (
    CALIBRATED_FORECAST_MEMO_ATTR,
    _memo_dict,
    _memo_entry,
    calibrated_forecast_key,
    calibrated_spot_for_period,
    calibrated_spot_map,
    interval_key_for_period,
)

Build = Callable[[Any], list[dict]]  # the entity's _calibrated_forecast_values
RunInExecutor = Callable[..., Awaitable[Any]]  # hass.async_add_executor_job


class WarmOutcome(enum.Enum):
    """What one ``CalibratedForecast.warm`` did."""

    NO_DATA = "no_data"
    HIT = "hit"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class CalibratedForecast:
    """One region's calibrated forecast memo: key, read, publish, lazy build, guarded warm, spot view.

    The memo lives on the coordinator, not on the entity (#35). The coordinator
    is per region and so is the calibrated forecast: PD7DayForecastSensor,
    SpotPriceForecastDays27Sensor and PD7DayDataSensor all read it for the same
    region with the same coordinator and the same calibration store, and they
    build each entry with the same computation. Memoising on the entity
    therefore ran the full recalibration of roughly 336 intervals three times
    per region instead of once, which is what made platform setup take 42 to
    53 seconds per region. The tariff sensors of the region read the same slot
    through the spot view (#62).

    The slot is ``{region: (key, list)}``. A missing or mismatched entry, or a
    None value, is a miss.
    """

    def __init__(self, coordinator: Any, store: Any, region: str) -> None:
        self._coordinator = coordinator
        self._store = store
        self._region = region

    # ── Forecast memo ────────────────────────────────────────────────────────

    def key(self, d: Any) -> tuple:
        """The memo key for PriceData ``d``; see ``calibration_inputs.calibrated_forecast_key``."""
        return calibrated_forecast_key(self._coordinator, self._store, self._region, d)

    def cached(self, key: tuple) -> list[dict] | None:
        """The memoised forecast for ``key``, or None if the memo does not hold it."""
        return _memo_entry(
            self._coordinator, CALIBRATED_FORECAST_MEMO_ATTR, self._region, key
        )

    def publish(self, key: tuple, value: list[dict]) -> None:
        """Store ``value`` under ``key`` in the region's slot, if the memo exists.

        Does not create the memo: only the lazy build does, as before.
        """
        cache = _memo_dict(self._coordinator, CALIBRATED_FORECAST_MEMO_ATTR)
        if cache is not None:
            cache[self._region] = (key, value)

    def is_current(self, d: Any | None) -> bool:
        """Whether the memo holds the value a write for ``d`` will ask for.

        True with no price data, because the write then cannot pay for a
        rebuild. There is no await here, so the answer cannot go stale between
        a caller's check and its state write, provided the caller has no await
        in between either.
        """
        if d is None:
            return True
        return self.cached(self.key(d)) is not None

    def forecast(self, d: Any, build: Build) -> list[dict]:
        """The calibrated forecast for ``d``: the memo on a hit, else ``build(d)`` stored under the key.

        This is the lazy fallback path and it runs on the event loop, so the
        key cannot move between the read and the write here: there is no await
        anywhere in it. The key is taken first and the result is published
        under that key. The memo dict is created on the coordinator when it is
        missing; a coordinator that refuses the attribute (a read-only mock)
        still gets the built value, just not memoised.
        """
        key = self.key(d)
        cache = _memo_dict(self._coordinator, CALIBRATED_FORECAST_MEMO_ATTR, create=True)
        cached = self.cached(key)
        if cached is not None:
            return cached
        value = build(d)
        if cache is not None:
            cache[self._region] = (key, value)
        return value

    async def warm(
        self,
        d: Any | None,
        build: Build,
        run: RunInExecutor,
        price_data_now: Callable[[], Any],
    ) -> WarmOutcome:
        """Populate the memo for ``d`` by running ``build`` through ``run``, off the event loop.

        The key is taken ONCE here, on the loop, before the executor hop, and
        then carried through to the publish. It used to be taken inside the
        executor job by ``_calibrated_forecast`` itself, which meant the warm
        did not know which key it had stored under and could not tell whether
        the world had moved while it was away. Two things went wrong with that
        (#60, PR #76):

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
        slot alone, and return SUPERSEDED; the entity's warm-until-current loop
        will come round again with the new key.

        Whatever ``run`` raises propagates to the caller, which logs it. The
        lazy path remains as the correctness fallback, so a failed warm costs
        speed, not data.
        """
        if d is None:
            return WarmOutcome.NO_DATA
        key = self.key(d)
        if self.cached(key) is not None:
            # Already warm for this key. Skipping the executor hop here is why
            # a hit costs nothing, which matters because every dispatch tick
            # routes five minute writes through this path.
            return WarmOutcome.HIT
        value = await run(build, d)
        if price_data_now() is not d or self.key(d) != key:
            # Superseded while we were in the executor. Publishing now would
            # label a stale list with a key that no longer describes it, and
            # could overwrite a fresher entry from a sibling entity.
            return WarmOutcome.SUPERSEDED
        self.publish(key, value)
        return WarmOutcome.PUBLISHED

    # ── Tariff spot view (#62) ───────────────────────────────────────────────

    def spot(self, period: Any, run_at: str | None) -> float | None:
        """Calibrated spot in $/kWh for ``period`` of the run stamped ``run_at``.

        The raw value when there is no calibration store; otherwise
        ``calibration_inputs.calibrated_spot_for_period``, None when the
        interval cannot be calibrated.
        """
        if not self._store:
            return period.value
        return calibrated_spot_for_period(self._store, self._coordinator, period, run_at)

    def spot_map(self, d: Any | None) -> dict | None:
        """The per run ``{interval key: calibrated spot}`` memo for the region, or None.

        None without a store or data. The key is taken here, on the loop, and
        passed down to ``calibration_inputs.calibrated_spot_map`` (the PR #76
        rule). May raise; the caller decides what a failure means.
        """
        if not self._store or d is None:
            return None
        key = self.key(d)
        return calibrated_spot_map(self._store, self._coordinator, self._region, d, key)

    def spot_memoised(self, period: Any, spot_map: dict | None) -> tuple[bool, float | None]:
        """``(True, spot)`` when ``spot_map`` carries ``period``, else ``(False, None)``.

        A miss is not evidence that the interval calibrates to nothing, so the
        caller falls through to calibrating it directly.
        """
        if spot_map is not None:
            interval_key = interval_key_for_period(period)
            if interval_key in spot_map:
                return True, spot_map[interval_key]
        return False, None
