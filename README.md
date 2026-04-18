# NEM PD7DAY Price Forecast — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-%3E%3D2024.1-blue.svg)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/github/v/release/purcell-lab/nem_pd7day)](https://github.com/purcell-lab/nem_pd7day/releases)

A Home Assistant custom integration that fetches AEMO's **7-day ahead electricity price forecasts** (PD7DAY) for the National Electricity Market (NEM) and exposes them as HA sensors with on-device machine-learning calibration.

AEMO publishes PD7DAY three times per day (07:30, 13:00, 18:00 AEST). This integration fetches those updates on the same schedule and applies an on-device calibration layer — using your local history of forecast vs actual prices — to produce calibrated estimates with P10/P50/P90 confidence bands.

---

## Features

- **7-day price forecast** — calibrated $/kWh for QLD1 (or any NEM region)
- **Confidence bands** — P10, P50, P90 quantile regression (IRLS) per forecast period
- **OLS calibration** — linear bias correction fitted on actual Amber vs PD7DAY pairs
- **Gas generation forecast** — daily TJ forecast from MARKET_SUMMARY
- **Interconnector flows** — NSW1-QLD1 and N-Q-MNSP1 MW forecasts
- **Market intervention flag** — binary sensor from CASESOLUTION data
- **Calibration diagnostic** — observation count, active bucket count, fit quality
- **No polling** — fetches only at AEMO publish times (3 requests/day)
- **Pure Python** — zero external dependencies beyond Home Assistant

---

## Requirements

- Home Assistant 2024.1 or later
- An [Amber Electric](https://www.amber.com.au/) account with the [Amber integration](https://www.home-assistant.io/integrations/ambee/) configured (required for calibration; the integration works in passthrough mode without it)
- Network access to `www.nemweb.com.au`

---

## Installation

### Via HACS (recommended)

1. Open HACS in your HA instance
2. Go to **Integrations → Custom repositories**
3. Add `https://github.com/purcell-lab/nem_pd7day` with category **Integration**
4. Search for **NEM PD7DAY** and install
5. Restart Home Assistant
6. Go to **Settings → Devices & Services → Add Integration** and search for **NEM PD7DAY**

### Manual

1. Download the latest release zip from the [Releases page](https://github.com/purcell-lab/nem_pd7day/releases)
2. Extract `custom_components/nem_pd7day/` into your HA config directory
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration** and search for **NEM PD7DAY**

---

## Configuration

The integration is configured via the UI config flow. You will be prompted for:

| Field | Default | Description |
|---|---|---|
| Region | `QLD1` | NEM region code (`QLD1`, `NSW1`, `VIC1`, `SA1`, `TAS1`) |
| Interconnectors | `NSW1-QLD1, N-Q-MNSP1` | Comma-separated interconnector IDs to monitor |

No `configuration.yaml` entries are required.

### Recorder exclusion (recommended)

The forecast sensors carry large attribute payloads (7 days × 48 intervals). Add the following to `configuration.yaml` to prevent recorder warnings and database bloat:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.pd7day_ic_*
    entities:
      - sensor.qld1_pd7day_forecast
      - sensor.qld1_pd7day_forecast_day_2_plus
      - sensor.qld1_pd7day_forecast_after_amber
      - sensor.qld1_pd7day_buy_cost_after_amber
```

---

## Sensors

### `sensor.qld1_pd7day_forecast`

The primary price forecast sensor.

| Attribute | Description |
|---|---|
| `state` | Calibrated price for the current interval ($/kWh) |
| `region` | NEM region code |
| `forecast_generated_at` | ISO-8601 timestamp of the AEMO source file |
| `forecast` | List of all forecast periods (see below) |
| `next_value` | Calibrated price for the next interval |
| `min_24h_value` | Minimum calibrated price in the next 24 hours |
| `max_24h_value` | Maximum calibrated price in the next 24 hours |
| `cheapest_2h_window` | Best contiguous 2-hour window over 7 days |

Each entry in `forecast` contains:

```yaml
nemtime: "2026-04-15T17:30:00+10:00"   # interval END (AEMO convention)
time:    "2026-04-15T17:00:00+10:00"   # interval START
raw_value: 0.084                        # raw AEMO forecast ($/kWh)
calibrated: 0.142                       # OLS-calibrated value
p10: 0.091                             # 10th percentile (optimistic)
p50: 0.138                             # 50th percentile (median)
p90: 0.231                             # 90th percentile (conservative)
mae: 0.038                             # mean absolute error of OLS fit
calibrated_source: ols                 # "ols" or "passthrough"
n_obs: 147                             # observations used for this bucket
horizon_hours: 36.5                    # hours ahead
value: 0.142                           # alias for calibrated (template compat)
```

> **Timestamp convention**: `nemtime` is the interval END timestamp as published by AEMO. `time` is the interval START (nemtime − 30 minutes). This matches the AEMO dispatch interval convention.

---

### `sensor.nem_pd7day_gas_forecast`

Gas-fired generation forecast from MARKET_SUMMARY.

| Attribute | Description |
|---|---|
| `state` | Gas generation for the current period (TJ/day) |
| `forecast` | List of daily gas forecast periods |

---

### `sensor.pd7day_ic_nsw1_qld1` / `sensor.pd7day_ic_n_q_mnsp1`

Interconnector flow forecasts.

| Attribute | Description |
|---|---|
| `state` | Current period MW flow (positive = export from QLD) |
| `interconnector_id` | Interconnector identifier |
| `forecast` | List of forecast periods with `nemtime`, `time`, `mw` |

---

### `binary_sensor.nem_pd7day_intervention`

`ON` when AEMO has flagged a market intervention in the CASESOLUTION data. Under normal market conditions this is `OFF`.

---

### `sensor.nem_pd7day_calibration`

Calibration system diagnostic sensor.

| Attribute | Description |
|---|---|
| `state` | Total observations logged |
| `active_buckets` | Number of calibration buckets with ≥ 10 observations |
| `total_buckets` | 24 (6 horizons × 4 time-of-day bands) |
| `fitted_at` | ISO-8601 timestamp of last model refit |
| `observation_count` | Same as state |

---

## Understanding AEMO Forecast Bias — The QLD Duck Curve

The core motivation for this integration's calibration layer is that AEMO's PD7DAY forecasts carry **systematic, predictable biases** that vary by time of day and forecast horizon. Understanding these biases helps set expectations for how much the calibration corrects and why.

![QLD Duck Curve and AEMO Forecast Bias](docs/qld_duck_curve_bias.png)

### The QLD1 Duck Curve

Queensland's electricity demand profile follows the characteristic "duck curve" shape driven by large-scale rooftop solar penetration:

```
Price (c/kWh)
    │
30+ │                                    ╭──╮  Evening peak
    │                                   ╱    ╲  (16–20h)
 20 │                                  ╱      ╲
    │            Morning              ╱        ╲
 10 │    ╭──╮    ramp          ╭────╯          ╰──╮ Shoulder
    │   ╱    ╲  (6–9h)        ╱                    ╲
  5 │──╯      ╰──────────────╯   ← solar trough     ╰──── Overnight
    │                           (10–16h, prices      baseload
  0 │────────────────────────────can go negative)
    └──────────────────────────────────────────────────────
    00:00                  12:00                    24:00
```

**Key features of the QLD1 price curve:**
- **Overnight** (22:00–06:00): Stable low baseload prices (~5–8 c/kWh)
- **Morning ramp** (06:00–10:00): Demand rises as businesses open; prices climb before solar onset
- **Solar trough** (10:00–16:00): Rooftop solar floods the grid, suppressing prices to near-zero or negative
- **Evening peak** (16:00–20:00): Solar falls off rapidly; demand stays high → sharp price spike
- **Evening shoulder** (20:00–22:00): Demand eases but generation still adjusting → elevated prices

### AEMO Forecast Biases (Empirically Observed)

After collecting paired forecast/actual observations, the calibration system reveals consistent systematic biases in AEMO's PD7DAY forecasts. These patterns are stable across multiple weeks of data:

#### Peak time-of-day (16:00–20:00)

This is the most dramatic bias. AEMO significantly over-forecasts evening peak prices at virtually all horizons:

| Horizon | OLS slope (a) | Interpretation |
|---|---|---|
| 0–6h ahead | 0.37 | AEMO forecasts ~2.7× too high |
| 6–12h ahead | 0.46 | AEMO forecasts ~2.2× too high |
| 12–24h ahead | ~0.10 | AEMO peak forecast carries almost no information |
| 24–48h ahead | ~0.04 | AEMO peak forecast carries no information |

At horizons beyond 12 hours, the calibrated output converges to a **flat intercept of ~$90/MWh** regardless of the raw AEMO forecast value. This is a structural market feature: AEMO's medium-range peak price forecasts are driven by operational risk conservatism rather than predictive accuracy, and the actual peak price clusters around a characteristic level independent of what AEMO forecasts.

This is consistent with independent academic findings. [Sinclair et al. (2026)](https://doi.org/10.3390/app16010075) found AEMO's pre-dispatch baseline achieves nMAPE of 57–122% at 2–16h horizons, compared to ~29–33% for transformer models trained on the same data. The SHAP analysis in that paper confirms that the pre-dispatch RRP forecast is the dominant input feature (>60% of model importance), but it contributes primarily as a signal to be corrected rather than trusted directly.

#### Solar time-of-day (10:00–16:00)

AEMO over-forecasts solar-period prices at shorter horizons, but this bias diminishes at longer horizons:

| Horizon | OLS slope (a) | Interpretation |
|---|---|---|
| 0–6h ahead | 0.91 | Mild over-forecast (~10%) |
| 6–12h ahead | 0.72 | Moderate over-forecast (~39%) |
| 12–24h ahead | 0.91 | Mild over-forecast, converging |
| 24–48h ahead | 0.98 | Near passthrough ✅ |

The 6–12h horizon is the worst zone for solar forecasting. This reflects a real physical limitation: intra-day cloud cover and solar generation ramp rates are highly variable and difficult to anticipate 6–12 hours ahead with grid-averaged forecasts.

The non-monotonic ordering (0–6h better than 6–12h) occurs because the 0–6h window captures significant overnight hours where solar generation is zero — a trivially easy forecast — which pulls the calibration coefficient toward 1.0.

#### Offpeak / overnight (22:00–10:00)

AEMO is most accurate for offpeak periods:

| Horizon | OLS slope (a) | Interpretation |
|---|---|---|
| 0–6h ahead | 0.87 | Mild over-forecast |
| 6–12h ahead | 0.99 | Near-perfect passthrough ✅ |
| 12–24h ahead | 1.04 | Mild under-forecast (3–4%) |
| 24–48h ahead | 0.85 | Moderate over-forecast |

The 6–12h offpeak window is the calibration system's most stable and accurate bucket.

#### Evening shoulder (20:00–22:00)

AEMO appears to systematically under-forecast prices in the transition period immediately after peak:

| Horizon | OLS slope (a) | Interpretation |
|---|---|---|
| 0–6h ahead | 1.00 | Perfect passthrough |
| 6–12h ahead | 1.41 | AEMO under-forecasts by ~41% |
| 12–24h ahead | 1.58 | AEMO under-forecasts by ~58% |
| 24–48h ahead | ~0.00 | Flat ~$84/MWh output |

The under-forecast bias at 6–24h ahead likely reflects AEMO's difficulty anticipating how long elevated evening demand persists into the shoulder period. These buckets have fewer observations and should be treated as provisional.

> **Note**: Coefficient values above are based on observed data from a QLD1 installation. Coefficients for other NEM regions will differ and will be fitted independently by the calibration system.

### Why This Matters for Home Automation

For battery storage dispatch, EV charging scheduling, and export timing decisions, the raw AEMO forecast can be significantly misleading:

- **Do not use raw AEMO peak forecasts** for scheduling decisions beyond 12 hours — they carry near-zero predictive information about actual peak prices
- **Calibrated forecasts** are most valuable in the solar trough window, where the correction is consistent and the absolute price level is low
- **P10/P90 bands** widen appropriately at longer horizons and during peak periods, reflecting genuine uncertainty — use them to schedule conservative vs aggressive battery strategies

---

## Calibration System

The calibration system corrects the known bias in AEMO's PD7DAY forecasts using your local history of forecast vs actual wholesale prices.

### How it works

1. **Forecast ingestion** — each fetch logs the forecast price for every future interval into persistent storage keyed by interval start time
2. **Actual logging** — when Amber's feed-in price sensor updates, the actual wholesale RRP is logged against the current interval
3. **Matching** — when both forecast and actual exist for an interval, an observation pair is created
4. **Bucketing** — observations are grouped into 24 buckets by horizon and time-of-day:

| Horizon buckets | Time-of-day buckets |
|---|---|
| `h00_06` — 0 to 6 hours ahead | `solar` — 10:00–16:00 |
| `h06_12` — 6 to 12 hours | `peak` — 16:00–20:00 |
| `h12_24` — 12 to 24 hours | `shoulder` — 20:00–22:00 |
| `h24_48` — 24 to 48 hours | `offpeak` — all other hours |
| `h48_96` — 48 to 96 hours | |
| `h96plus` — beyond 96 hours | |

5. **Model fitting** — once a bucket has ≥ 10 observations, two models are fitted:
   - **OLS** (ordinary least squares): `calibrated = a × raw + b` — corrects linear bias
   - **IRLS quantile regression** (pinball loss): separate fits for P10, P50, P90

6. **Application** — at forecast time, each period's bucket is looked up. If active, OLS and quantile values are returned. Otherwise the raw value passes through unchanged.

### Warm-up period

With 3 fetches per day, expect:

| Day | Buckets active | Coverage |
|---|---|---|
| 1–3 | 0–8 | Short-horizon calibration beginning |
| 4–5 | 12–16 | Near-term (0–48h) calibrated |
| 7–10 | 16–20 | h48_96 activating |
| 12–16 | 20–24 | Full 7-day calibration |

> **Note**: Shoulder buckets (20:00–22:00, only 2 hours wide) accumulate observations more slowly than other bands and may take 2–3 weeks to reach reliable coefficient estimates.

### Persistent storage

All three storage files are required for full calibration continuity across HA restarts:

| File | Contents |
|---|---|
| `/config/.storage/nem_pd7day.observation_log` | Paired forecast/actual observations |
| `/config/.storage/nem_pd7day.calibration_coefficients` | Fitted OLS + quantile regression models |
| `/config/.storage/nem_pd7day.forecast_history` | Forecast entries for all future intervals (enables h48_96+ matching after restart) |

To reset calibration (e.g. after changing regions):

```bash
rm /config/.storage/nem_pd7day.observation_log
rm /config/.storage/nem_pd7day.calibration_coefficients
rm /config/.storage/nem_pd7day.forecast_history
```

Then reload or restart the integration.

> **Why three files?** The `forecast_history` file stores AEMO's forecasts for all intervals up to 7 days ahead. Without persisting this, a Home Assistant restart would lose the forecast context for intervals more than ~6 hours ahead — preventing observations from being recorded for h48_96 and h96plus buckets.

---

## Fetch Schedule

| NEM time (AEST) | UTC | Notes |
|---|---|---|
| 07:30 | 21:30 (previous day) | Morning AEMO publish |
| 13:00 | 03:00 | Midday AEMO publish |
| 18:00 | 08:00 | Evening AEMO publish |

The integration uses `async_track_point_in_utc_time` which fires reliably at exact UTC datetimes and self-reschedules 24 hours after each fire. It works correctly regardless of the HA host timezone.

---

## Template Sensor Examples

The `value` key in each forecast period is an alias for `calibrated` (or `raw_value` in passthrough), maintained for backward compatibility with template sensors.

### Next 24-hour minimum price

```yaml
template:
  - sensor:
      - name: "PD7DAY Min Price 24h"
        unit_of_measurement: "$/kWh"
        state: >
          {{ state_attr('sensor.qld1_pd7day_forecast', 'min_24h_value') | round(4) }}
```

### Cheapest 2-hour window start time

```yaml
template:
  - sensor:
      - name: "PD7DAY Cheapest Window Start"
        state: >
          {{ state_attr('sensor.qld1_pd7day_forecast', 'cheapest_2h_window')['time'] }}
```

### Calibration bucket table (Markdown card)

```yaml
type: markdown
content: >
  ## Calibration Buckets

  | Bucket | n | Status |
  |---|---|---|
  {% for name, b in state_attr('sensor.nem_pd7day_calibration', 'summary').buckets.items() %}
  {% if b.n >= 10 %}
  | {{ name }} | {{ b.n }} | <span style='background:#1a7f37;color:#fff;padding:2px 8px;border-radius:10px;font-size:0.85em'>✅ OLS</span> |
  {% elif b.n > 0 %}
  | {{ name }} | {{ b.n }} | <span style='background:#9a6700;color:#fff;padding:2px 8px;border-radius:10px;font-size:0.85em'>🔶 {{ b.n }}/10</span> |
  {% else %}
  | {{ name }} | {{ b.n }} | <span style='background:#cf222e;color:#fff;padding:2px 8px;border-radius:10px;font-size:0.85em'>⬜ 0/10</span> |
  {% endif %}
  {% endfor %}
```

---

## NEM Time Convention

All timestamps in this integration use **AEST (UTC+10:00)** with no daylight saving adjustment, matching AEMO's published data. Timestamps are always ISO-8601 with explicit `+10:00` suffix, e.g. `2026-04-14T07:30:00+10:00`.

The `nemtime` field in forecast periods is the **interval end** timestamp (AEMO convention). The `time` field is the **interval start** (`nemtime − 30 minutes`).

---

## Data Source

Price forecast data is sourced from [AEMO NEMWeb](https://www.nemweb.com.au/REPORTS/CURRENT/PD7Day/) — the Australian Energy Market Operator's public data portal. The PD7DAY dataset is updated three times per day and contains 7-day ahead dispatch price forecasts for all NEM regions.

The calibration methodology is informed by academic research into NEM price forecasting, particularly [Sinclair et al. (2026)](https://doi.org/10.3390/app16010075) which demonstrates that AEMO's pre-dispatch forecast is the dominant signal in NEM price formation (>60% of ML model feature importance by SHAP analysis), while also confirming its systematic biases at different horizons.

---

## Troubleshooting

### Integration fails to load

Check the HA log for errors from `custom_components.nem_pd7day`. The most common cause is a network issue reaching `nemweb.com.au`.

### Sensors show `unavailable`

The first fetch runs at integration load. Check **Settings → System → Logs** filtered to `nem_pd7day` for fetch errors.

### p10/p50/p90 values are `null`

Normal for the first 3–5 days. Calibration requires at least 10 observations per bucket. Check `sensor.nem_pd7day_calibration` state for current observation count.

### h48_96 / h96plus buckets remain at n=0 after a week

If these buckets are not accumulating observations, the `nem_pd7day.forecast_history` storage file may be missing (pre-v1.9.1 installations). Delete the observation log and restart to rebuild:

```bash
rm /config/.storage/nem_pd7day.observation_log
rm /config/.storage/nem_pd7day.forecast_history
```

Then reload the integration. Observations will restart from zero but h48_96 should begin accumulating within 2–3 days.

### Recorder warnings about attribute size

Add the recorder exclusions shown in the [Configuration](#configuration) section.

---

## Version History

| Version | Changes |
|---|---|
| 1.9.1 | Fix: persist `forecast_history` to `.storage` — prevents h48_96/h96plus buckets remaining at n=0 after HA restart |
| 1.9.0 | Comprehensive test suite (107 tests), HACS validation fixes, brand assets |
| 1.8.0 | Dedup fix: skip ingesting same `run_at` twice; prevents observation count drift |
| 1.7.0 | Horizon consistency fix: both calibration and actual-recording use interval START time |
| 1.6.0 | Quantile regression (IRLS P10/P50/P90), sanity guards on OLS coefficients |
| 1.5.0 | AEMO interval convention: `nemtime` (end) + `time` (start) on all forecast periods |
| 1.4.0 | Timezone overhaul: all timestamps ISO-8601 +10:00, UTC-safe scheduling |
| 1.3.0 | Replaced polling with `async_track_point_in_utc_time` at AEMO publish times |
| 1.2.0 | OLS + IRLS quantile calibration engine, Amber listener, calibration diagnostic sensor |
| 1.1.0 | CASESOLUTION (binary sensor), MARKET_SUMMARY (gas), INTERCONNECTORSOLUTION sensors |
| 1.0.0 | Initial release: PRICESOLUTION forecast sensor |

---

## License

MIT License — see [LICENSE](LICENSE) for details.
