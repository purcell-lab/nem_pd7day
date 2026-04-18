# NEM PD7DAY Price Forecast — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-%3E%3D2024.1-blue.svg)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/github/v/release/purcell-lab/nem_pd7day)](https://github.com/purcell-lab/nem_pd7day/releases)

A Home Assistant custom integration that fetches AEMO's **7-day ahead electricity price forecasts** (PD7DAY) for the National Electricity Market (NEM) and exposes them as HA sensors with machine-learning calibration.

AEMO publishes PD7DAY three times per day (07:30, 13:00, 18:00 AEST). This integration fetches those updates on the same schedule and applies an on-device calibration layer — using your local history of forecast vs actual prices — to produce calibrated estimates with P10/P50/P90 confidence bands.

---

## Features

- **Multi-region support** — track any combination of QLD1, NSW1, VIC1, SA1, TAS1
- **7-day price forecast** — calibrated $/kWh per region
- **Confidence bands** — P10, P50, P90 quantile regression (IRLS) per forecast period
- **OLS calibration** — linear bias correction fitted on actual TradingIS vs PD7DAY pairs
- **Gas generation forecast** — daily TJ forecast from MARKET_SUMMARY
- **Interconnector flows** — per-region interconnector MW forecasts
- **Market intervention flag** — binary sensor from CASESOLUTION data
- **Calibration diagnostic** — observation count, active bucket count, fit quality
- **Forecast history diagnostic** — monitor forecast history storage health
- **No polling** — fetches only at AEMO publish times (3 requests/day)
- **No third-party accounts required** — actual prices sourced directly from AEMO TradingIS
- **Pure Python** — zero external dependencies beyond Home Assistant

---

## Requirements

- Home Assistant 2024.1 or later
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

The integration is configured via the UI config flow in a single step.

**Step 1 — Regions**

| Field | Default | Description |
|---|---|---|
| NEM Regions | QLD1 | One or more of QLD1, NSW1, VIC1, SA1, TAS1 |

The first selected region is automatically used as the calibration region. Multi-region support means all regions get forecast sensors, but calibration (actual price matching) applies only to the primary region.

> Actual prices are sourced directly from AEMO's TradingIS reports on NEMWeb — no third-party account required. The integration fetches actual 5-minute dispatch prices every 30 minutes and automatically matches them against forecast history to build calibration.

No `configuration.yaml` entries are required.

### Recorder exclusion (recommended)

The forecast sensors carry large attribute payloads (7 days x 48 intervals per region). Add the following to `configuration.yaml` to prevent recorder warnings and database bloat:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.pd7day_*_ic_*
      - sensor.*_pd7day_forecast
```

---

## Actual price sourcing

From v2.0.0, actual prices are fetched directly from AEMO's TradingIS dispatch reports on NEMWeb:

- **URL**: `https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/`
- **Schedule**: every 30 minutes, at HH:02 and HH:32 NEM time (2 minutes after each trading interval boundary)
- **Method**: downloads the 6 x 5-minute dispatch files for each trading interval, averages the RRP values
- **Observation tagging**: each observation records `actual_source: "tradingis"` for auditability

No Amber Electric account or sensor is required.

---

## Sensors

### `sensor.{region}_pd7day_forecast`

The primary price forecast sensor (one per configured region).

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

> **Timestamp convention**: `nemtime` is the interval END timestamp as published by AEMO. `time` is the interval START (nemtime - 30 minutes). This matches the AEMO dispatch interval convention.

---

### `sensor.nem_pd7day_gas_forecast`

Gas-fired generation forecast from MARKET_SUMMARY.

| Attribute | Description |
|---|---|
| `state` | Gas generation for the current period (TJ/day) |
| `forecast` | List of daily gas forecast periods |

---

### `sensor.pd7day_{region}_ic_{interconnector}`

Interconnector flow forecasts (one per region-interconnector pair).

| Attribute | Description |
|---|---|
| `state` | Current period MW flow (positive = export) |
| `interconnector_id` | Interconnector identifier |
| `forecast` | List of forecast periods with `nemtime`, `time`, `mw` |

---

### `binary_sensor.nem_pd7day_{region}_intervention`

`ON` when AEMO has flagged a market intervention in the CASESOLUTION data. Under normal market conditions this is `OFF`.

---

### `sensor.nem_pd7day_{region}_calibration`

Calibration system diagnostic sensor.

| Attribute | Description |
|---|---|
| `state` | Total observations logged |
| `active_buckets` | Number of calibration buckets with >= 10 observations |
| `total_buckets` | 24 (6 horizons x 4 time-of-day bands) |
| `fitted_at` | ISO-8601 timestamp of last model refit |
| `observation_count` | Same as state |

---

### `sensor.nem_pd7day_{region}_forecast_history`

Forecast history storage diagnostic sensor.

| Attribute | Description |
|---|---|
| `state` | Total number of forecast entries in storage across all tracked intervals |
| `interval_keys` | Number of unique intervals tracked |
| `oldest_interval` | ISO-8601 timestamp of the oldest tracked interval |
| `newest_interval` | ISO-8601 timestamp of the newest tracked interval |
| `runs_per_interval_avg` | Average number of forecast runs per interval |
| `storage_key` | HA storage key for the forecast history store |

**Purpose**: monitor the health and size of the forecast history store; useful for verifying that h48_96/h96plus buckets are accumulating entries after the v1.9.1 persistence fix.

---

### `sensor.nem_pd7day_{region}_source_file_datetime`

Per-region diagnostic timestamp showing when the latest AEMO source file was published.

---

### `sensor.nem_pd7day_{region}_data_updated_datetime`

Per-region diagnostic timestamp showing when the coordinator last refreshed data.

---

## Calibration System

The calibration system corrects the known bias in AEMO's PD7DAY forecasts using your local history of forecast vs actual wholesale prices.

### How it works

1. **Forecast ingestion** — each fetch logs the forecast price for every future interval into persistent storage keyed by interval start time
2. **Actual logging** — at HH:02 and HH:32, TradingIS dispatch prices are fetched from AEMO and logged against the just-closed interval
3. **Matching** — when both forecast and actual exist for an interval, an observation pair is created
4. **Bucketing** — observations are grouped into 24 buckets by horizon and time-of-day:

| Horizon buckets | Time-of-day buckets |
|---|---|
| `h00_06` — 0 to 6 hours ahead | `solar` — 10:00-16:00 |
| `h06_12` — 6 to 12 hours | `peak` — 16:00-20:00 |
| `h12_24` — 12 to 24 hours | `shoulder` — 20:00-22:00 |
| `h24_48` — 24 to 48 hours | `offpeak` — all other hours |
| `h48_96` — 48 to 96 hours | |
| `h96plus` — beyond 96 hours | |

5. **Model fitting** — once a bucket has >= 10 observations, two models are fitted:
   - **OLS** (ordinary least squares): `calibrated = a + b x raw` — corrects linear bias
   - **IRLS quantile regression** (pinball loss): separate fits for P10, P50, P90

6. **Application** — at forecast time, each period's bucket is looked up. If active, OLS and quantile values are returned. Otherwise the raw value passes through unchanged.

### Warm-up period

With 3 fetches per day, expect:

| Day | Buckets active | Coverage |
|---|---|---|
| 1-3 | 0 | All passthrough |
| 4-5 | h00_06, h06_12, h12_24 | Near-term calibrated |
| 6-8 | h24_48 | 2-day horizon calibrated |
| 10-14 | h48_96, h96plus | Full 7-day calibration |

### Storage files

| File | Contents |
|---|---|
| `nem_pd7day.observation_log` | All calibration observations (forecast vs actual pairs) |
| `nem_pd7day.calibration_coefficients` | Fitted OLS and quantile regression models per bucket |
| `nem_pd7day.forecast_history` | Running forecast history indexed by interval time — enables calibration across HA restarts |

To reset calibration (e.g. after changing regions):

```bash
rm /config/.storage/nem_pd7day.observation_log
rm /config/.storage/nem_pd7day.calibration_coefficients
rm /config/.storage/nem_pd7day.forecast_history
```

Then reload or restart the integration.

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

---

## NEM Time Convention

All timestamps in this integration use **AEST (UTC+10:00)** with no daylight saving adjustment, matching AEMO's published data. Timestamps are always ISO-8601 with explicit `+10:00` suffix, e.g. `2026-04-14T07:30:00+10:00`.

The `nemtime` field in forecast periods is the **interval end** timestamp (AEMO convention). The `time` field is the **interval start** (`nemtime - 30 minutes`).

---

## Data Source

Price forecast data is sourced from [AEMO NEMWeb](https://www.nemweb.com.au/REPORTS/CURRENT/PD7Day/) — the Australian Energy Market Operator's public data portal. The PD7DAY dataset is updated three times per day and contains 7-day ahead dispatch price forecasts for all NEM regions.

Actual prices are sourced from [AEMO TradingIS Reports](https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/) — 5-minute dispatch price data published every 5 minutes.

---

## Troubleshooting

### Integration fails to load

Check the HA log for errors from `custom_components.nem_pd7day`. The most common cause is a network issue reaching `nemweb.com.au`.

### Sensors show `unavailable`

The first fetch runs at integration load. Check **Settings → System → Logs** filtered to `nem_pd7day` for fetch errors.

### p10/p50/p90 values are `null`

Normal for the first 3-5 days. Calibration requires at least 10 observations per bucket. Check `sensor.nem_pd7day_{region}_calibration` state for current observation count.

### Recorder warnings about attribute size

Add the recorder exclusions shown in the [Configuration](#configuration) section.

---

## Version History

| Version | Changes |
|---|---|
| 2.0.2 | Clean sensor names (remove duplicate region prefix), remove Amber dependency, add forecast history sensor |
| 2.0.1 | Version bump |
| 2.0.0 | Multi-region support, TradingIS actual prices, forecast persistence |
| 1.9.0 | Comprehensive pre-deployment test suite (107 tests) |
| 1.5.0 | AEMO interval convention: `nemtime` (end) + `time` (start) on all forecast periods |
| 1.4.0 | Timezone overhaul: all timestamps ISO-8601 +10:00, UTC-safe scheduling |
| 1.3.0 | Replaced polling with `async_track_point_in_utc_time` at AEMO publish times |
| 1.2.0 | OLS + IRLS quantile calibration engine, Amber listener, calibration diagnostic sensor |
| 1.1.0 | CASESOLUTION (binary sensor), MARKET_SUMMARY (gas), INTERCONNECTORSOLUTION sensors |
| 1.0.0 | Initial release: PRICESOLUTION forecast sensor |

---

## License

MIT License — see [LICENSE](LICENSE) for details.
