# QLD1 experimental scarcity premium

Separate additive sensor; no existing prices, tariffs or HAEO settings are
changed. Not a calibrated scarcity model, price-spike probability, or validated
battery-control rule. Use in shadow evaluation first.

## Entity contract

The QLD1 device gains **NEM Scarcity Premium**. Its entity ID is assigned by
Home Assistant; use the entity on the QLD1 device rather than assuming an ID.
The `base_entity` attribute identifies the exact calibrated forecast to which
this premium applies. The sensor state is the current half-hour addition.

```yaml
unit_of_measurement: "$/kWh"
interpolation_mode: previous
interval_minutes: 30
forecast:
  - time: "2026-09-23T00:00:00+00:00"
    value: 0.031
  - time: "2026-09-23T00:30:00+00:00"
    value: 0.021
```

Illustrative values, not a live observation. Each timestamp is an interval
START in UTC; application hours are fixed NEM time, UTC+10 without DST.
The series has 336 half-hour intervals plus a terminal zero boundary, covering
seven days from the current half-hour. Step interpolation prevents ramps
across boundaries. Zero outside today's application window means this policy
does not apply a premium, not a prediction that later days have no scarcity:
the signal describes the market now, not then.

## Explicit heuristic

1. Take the QLD1 dispatch prices (ELEC_NEM_SUMMARY, one per five-minute
   interval, already fetched by the integration's dispatch coordinator) whose
   settlement ends fall in the last 30 minutes: at most six. Average them.
2. Use that mean only while its newest price is at most 10 minutes old.
3. Activate if the mean is strictly above $30/MWh.
4. Target floor = lesser of that mean and $65/MWh.
5. For today's 10:00 to 14:00 half-hours, from the current one on:
   `premium = min(65, max(0, target_floor - base_forecast))`, in $/MWh.
6. Divide by 1,000 for the published $/kWh series. Outside that window, zero.

The mean rolls: it is recomputed on every dispatch update and on a one-minute
clock, so through the window the floor follows the market of the last half
hour rather than a figure fixed at 10:00.

Example: a recent mean of $41/MWh and base forecast $10/MWh produces a $31/MWh
addition, published as `0.031`. If the forecast rises to $45/MWh, the premium
is zero. The $65/MWh addition cap also applies to negative base prices, so the
target floor is not guaranteed to be reached on deeply negative intervals.

The trigger, floor cap, premium cap, 30-minute window and 10-minute freshness
limit are named constants in `scarcity_premium.py`. They are policy choices,
not fitted coefficients. The previous exploratory backtest did not establish
an expected additive premium: it mixed interval resolutions, included an
incomplete day in some statistics, and used an in-sample daytime price
threshold rather than a forecast-error outcome. Its reported precision/recall
are not deployment validation.

### Why 30 minutes, not the 07:00-10:00 morning

The first version averaged all 36 dispatch prices from 07:00 to 10:00 and
used nothing unless every one had been observed live. A restart or a missed
interval during that morning left the sensor unavailable for the whole
10:00-14:00 window, as happened on 28 September 2026. The last 30 minutes of
ELEC_NEM_SUMMARY is a simpler approximation of the same "is the market
running hot" signal, and it recovers within one dispatch interval.

## Availability and persistence

The sensor reuses the integration's existing five-minute dispatch coordinator;
it makes no additional market requests. Prices are deduplicated by settlement
end, kept for 30 minutes, saved to Home Assistant storage and restored after
a restart. A partial window averages the prices present; `signal_samples`
says how many. With no price newer than 10 minutes inside the application
window the sensor is unavailable (`no_recent_dispatch`), not a zero premium;
one fresh dispatch price makes it available again.

An active signal also requires a fresh calibrated base forecast with every
remaining application interval present. A stale forecast, missing interval or
invalid numeric value suppresses the entire series. Before 10:00 and after
14:00, zero is an explicit inactive policy result. One-minute clock updates
ensure the 14:00 and midnight transitions do not depend on receipt of a new
market forecast.

`status`, `signal_samples`, `signal_window_minutes`,
`signal_freshness_minutes`, `signal_source`, `signal_mean_mwh`,
`target_floor_mwh` and `experimental` expose the decision inputs. The large
forecast attribute is excluded from recorder history.

## HAEO connection

HAEO accepts a list of `{time, value}` points with a unit and
`interpolation_mode: previous`, and sums multiple forecast inputs. See its
[parser](https://github.com/purcell-lab/haeo/blob/main/custom_components/haeo/core/data/loader/extractors/haeo.py)
and [sensor guide](https://github.com/purcell-lab/haeo/blob/main/docs/user-guide/forecasts-and-sensors.md).

Use the `base_entity` forecast plus this entity in a shadow HAEO price input.
Do not also add an already premium-adjusted price: that double counts.
Do not directly add it to a different Amber forecast and assume the gap is
unchanged. It is a wholesale overlay, excluding network charges, margins,
loss factors and GST; a retail tariff must convert the adjusted wholesale
price consistently. No automatic production HAEO rewiring is included.

## Deployment boundary

This change adds only a QLD1 entity and supporting code/tests. It does not
change the integration version, tag a release, merge a branch, restart Home
Assistant, or activate a battery controller. Deployment and shadow observation
remain separate from live control activation.

## Verification

Built against repository commit `63e400410195bef961789b74a2f0b50def1b3148`.
Full repository pytest run: 1,278 passed, 9 skipped. Ruff checks for the new
Python files and `git diff --check` passed. Tests cover policy boundaries,
five-minute settlement-end handling, duplicate observations, invalid values,
forecast catch-up, capped negative-price uplift, missing/stale inputs,
midnight reset, restart restoration, unload persistence and QLD-only
registration. Adapter tests use the repository's Home Assistant stubs.
This is code-level verification, not a live HA/HAEO deployment test or a
market-performance backtest.
