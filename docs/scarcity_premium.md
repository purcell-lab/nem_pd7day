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
does not apply a premium, not a prediction that tomorrow has no scarcity.

## Explicit heuristic

1. Capture all 36 unique five-minute dispatch prices for the physical window
   07:00 to 10:00 NEM time. Dispatch interval ends are 07:05 through 10:00,
   inclusive. Average them once each. No partial windows or future samples.
2. Only after the complete window is available, activate if the mean is
   strictly above $30/MWh.
3. Target floor = lesser of that morning mean and $65/MWh.
4. For today's 10:00 to 14:00 half-hours:
   `premium = min(65, max(0, target_floor - base_forecast))`, in $/MWh.
5. Divide by 1,000 for the published $/kWh series. Outside that window, zero.

Example: morning mean $41/MWh and base forecast $10/MWh produces a $31/MWh
addition, published as `0.031`. If the forecast rises to $45/MWh, the premium
is zero. The $65/MWh addition cap also applies to negative base prices, so the
target floor is not guaranteed to be reached on deeply negative intervals.

The trigger, floor cap and premium cap are named constants in
`scarcity_premium.py`. They are policy choices, not fitted coefficients.
The previous exploratory backtest did not establish an expected additive
premium: it mixed interval resolutions, included an incomplete day in some
statistics, and used an in-sample daytime price threshold rather than a
forecast-error outcome. Its reported precision/recall are not deployment
validation. The 07:00-10:00 signal is not available at 07:00.

## Availability and persistence

The sensor reuses the integration's existing five-minute dispatch coordinator;
it makes no additional market requests. Samples are deduplicated by settlement
end, saved to Home Assistant storage, restored after restart and reset by NEM
calendar date. It must run through the full morning window; there is no
historical backfill. First installation after 07:05 or a missed interval can
therefore make it unavailable during today's 10:00-14:00 window.

Incomplete observations are unavailable during the application window, not
a zero premium. An active signal also requires a fresh calibrated base
forecast with every remaining application interval present. A stale forecast,
missing interval or invalid numeric value suppresses the entire series.
Before 10:00 and after 14:00, zero is an explicit inactive policy result.
One-minute clock updates ensure the 14:00 and midnight transitions do not
depend on receipt of a new market forecast.

`status`, `morning_samples`, `required_samples`, `morning_mean_mwh`,
`target_floor_mwh`, and `experimental` expose the decision inputs. The large
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
