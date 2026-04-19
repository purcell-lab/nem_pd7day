# NEM PD7DAY Price Forecast — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-%3E%3D2024.1-blue.svg)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/github/v/release/purcell-lab/nem_pd7day)](https://github.com/purcell-lab/nem_pd7day/releases)

A Home Assistant custom integration that fetches AEMO's **7-day ahead electricity price forecasts** (PD7DAY) for the National Electricity Market (NEM) and exposes them as HA sensors with machine-learning calibration.

AEMO publishes PD7DAY three times per day (07:30, 13:00, 18:00 AEST). This integration fetches those updates on the same schedule and applies an on-device calibration layer — using your local history of forecast vs actual prices — to produce calibrated estimates with P10/P50/P90 confidence bands.

---

## Features

- **7-day price forecast** — calibrated $/kWh with P10/P50/P90 confidence bands
- **OLS calibration** — linear bias correction fitted on actual TradingIS vs PD7DAY pairs
- **Gas generation forecast** — daily TJ forecast from MARKET_SUMMARY
- **Interconnector flows** — interconnector MW flow forecasts for the configured region
- **Market intervention flag** — binary sensor from CASESOLUTION data
- **Calibration diagnostic** — observation count, active bucket count, fit quality per bucket
- **Cloud polling** — two independent polling loops:
  - **PD7DAY** fetches at AEMO publish times: 07:30, 13:00, 18:00 AEST (3 requests/day)
  - **TradingIS** fetches actual 5-min dispatch prices every 30 minutes (48 requests/day)
- **Live sensor state** — all forecast sensor states advance automatically every 30 minutes to reflect the current interval, with no fetch required
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

The integration is configured via a single-step UI flow.

| Field | Default | Description |
|---|---|---|
| NEM Region | QLD1 | The NEM region to monitor: `QLD1`, `NSW1`, `VIC1`, `SA1`, or `TAS1` |

The configured region is used for both price forecasting and calibration. No additional settings are required.

No `configuration.yaml` entries are required.

### Monitoring multiple regions

Each integration instance monitors one NEM region with full independent calibration. To monitor multiple regions, add a separate integration instance for each via **Settings → Integrations → Add Integration → NEM PD7DAY**. Each instance maintains its own calibration store, observation log, and forecast sensors.

### Upgrading from v2.0.0–v2.0.2

Those versions supported multi-region configuration. From v2.0.3 onwards exactly one region is configured per integration instance. On upgrade, only the first region from a previous multi-region list is preserved — the integration migrates automatically. To reinstate additional regions, add new integration instances as described above.

### Recorder exclusion (recommended)

The forecast sensors carry large attribute payloads (7 days × 48 intervals). Add the following to `configuration.yaml` to prevent recorder warnings and database bloat:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.pd7day_*_ic_*
      - sensor.*_pd7day_forecast
```

---

## Data sources and polling

The integration runs two independent polling loops.

### PD7DAY forecasts (3×/day)

AEMO publishes 7-day ahead price forecasts three times per day. The integration fetches on the same schedule using `async_track_point_in_utc_time`:

| NEM time (AEST) | UTC |
|---|---|
| 07:30 | 21:30 (previous day) |
| 13:00 | 03:00 |
| 18:00 | 08:00 |

Source: [AEMO NEMWeb PD7DAY](https://www.nemweb.com.au/REPORTS/CURRENT/PD7Day/)

### TradingIS actual prices (every 30 minutes)

Actual NEM dispatch prices are fetched from AEMO's TradingIS reports and used to build calibration observations:

- **URL**: `https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/`
- **Schedule**: at HH:02 and HH:32 NEM time — 2 minutes after each 30-minute trading interval closes
- **Method**: downloads the 6 × 5-minute dispatch files for the closed interval, averages the RRP values ($/MWh → $/kWh)
- **Observation tagging**: each calibration observation records `actual_source: "tradingis"`

### Sensor state updates (every 30 minutes)

Forecast sensor states — price forecast, interconnector flow, and gas generation — advance automatically at each 30-minute interval boundary (:00 and :30 past every hour). The state always reflects the current interval from the most recent fetch, without waiting for the next PD7DAY fetch. This means:

- After the 07:30 fetch, the price forecast sensor state will step through each 30-minute interval for the rest of the day
- After HA restart, the state reflects the current interval immediately on load
- Between fetches, the raw forecast data is unchanged — only the *active interval* advances

---

## Sensors

All sensors are grouped under a single HA device named **NEM PD7DAY {region}** (e.g. `NEM PD7DAY QLD1`).

### Price Forecast

`sensor.nem_pd7day_{region}_price_forecast` — the primary calibrated price forecast sensor.

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

> **Timestamp convention**: `nemtime` is the interval END timestamp as published by AEMO. `time` is the interval START (`nemtime − 30 minutes`). This matches the AEMO dispatch interval convention.

---

### Gas Generation Forecast

`sensor.nem_pd7day_gas_forecast` — gas-fired generation forecast from MARKET_SUMMARY.

| Attribute | Description |
|---|---|
| `state` | Gas generation for the current period (TJ/day) |
| `forecast` | List of daily gas forecast periods |

---

### Interconnector Flow

`sensor.pd7day_{region}_ic_{interconnector}` — one sensor per interconnector touching the configured region.

| Attribute | Description |
|---|---|
| `state` | Current period MW flow (positive = export) |
| `interconnector_id` | Interconnector identifier |
| `forecast` | List of forecast periods with `nemtime`, `time`, `mw` |

Default interconnectors per region:

| Region | Interconnectors |
|---|---|
| QLD1 | NSW1-QLD1, N-Q-MNSP1 |
| NSW1 | NSW1-QLD1, VIC1-NSW1, N-Q-MNSP1 |
| VIC1 | VIC1-NSW1, SA1-VIC1, V-S-MNSP1, T-V-MNSP1 |
| SA1 | V-SA, V-S-MNSP1 |
| TAS1 | T-V-MNSP1 |

---

### Market Intervention

`binary_sensor.nem_pd7day_{region}_intervention` — `ON` when AEMO has flagged a market intervention in the CASESOLUTION data. Under normal market conditions this is `OFF`.

---

### Calibration

`sensor.nem_pd7day_{region}_calibration` — calibration system diagnostic. This sensor is in the **Diagnostic** category and is hidden from the default HA dashboard (visible under the device's Diagnostic section).

| Attribute | Description |
|---|---|
| `state` | Total observations logged |
| `active_buckets` | Buckets with ≥ 10 observations (max 24) |
| `total_buckets` | 24 (6 horizons × 4 time-of-day bands) |
| `fitted_at` | ISO-8601 timestamp of last model refit |
| `summary` | Per-bucket coefficients, MAE, RMSE, quantile slopes |

#### Forecast history attributes

| Attribute | Description |
|---|---|
| `forecast_history_entries` | Total forecast entries stored across all tracked intervals |
| `forecast_history_intervals` | Number of unique interval keys in storage |
| `forecast_history_oldest` | ISO-8601 timestamp of the oldest tracked interval |
| `forecast_history_newest` | ISO-8601 timestamp of the most recent tracked interval |
| `forecast_history_runs_avg` | Average number of forecast runs per interval |

Use these to verify h48_96/h96plus buckets are accumulating — `forecast_history_entries` should grow by ~3 per interval per day (one entry per AEMO fetch).

---

### Source File Datetime / Data Updated

- `sensor.nem_pd7day_{region}_source_file_datetime` — timestamp of the latest AEMO PD7DAY source file
- `sensor.nem_pd7day_{region}_data_updated` — timestamp of the last coordinator data refresh

Both are diagnostic sensors (EntityCategory.DIAGNOSTIC) and do not appear on the default dashboard.

---

## Calibration System

The calibration system corrects the known bias in AEMO's PD7DAY forecasts using your local history of forecast vs actual wholesale prices.

### How it works

1. **Forecast ingestion** — each fetch logs the forecast price for every future interval into persistent storage, keyed by interval start time
2. **Actual logging** — at HH:02 and HH:32, TradingIS dispatch prices are fetched from AEMO and logged against the just-closed interval
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
   - **IRLS quantile regression** (pinball loss): separate P10, P50, P90 slope fits

6. **Application** — at forecast time, each period's bucket is looked up. If active, OLS and quantile values are returned. Otherwise the raw value passes through unchanged.

### AEMO forecast bias — QLD empirical findings

![QLD Duck Curve and AEMO Bias Analysis](docs/qld_duck_curve_bias.png)

The chart above shows three panels derived from 648 forecast vs actual observation pairs collected over the first five days of operation (Apr 15–19 2026, QLD1):

**Panel 1 — Duck curve**: The stylised QLD wholesale price profile. Actual prices follow the classic duck curve shape — a solar-driven trough around 13:00 and a sharp evening ramp as rooftop solar drops off and demand peaks around 18:30. AEMO's raw PD7DAY forecast over-estimates the evening peak height and under-corrects the solar trough depth. The calibrated forecast applies the OLS correction to bring both in line.

**Panel 2 — OLS slope heatmap**: Each cell shows the fitted slope `a` for that horizon × time-of-day bucket. Green (a≈1) means AEMO's forecast is unbiased. Red (a<1) means AEMO over-forecasts and the calibration compresses the signal. Yellow-green (a>1) means AEMO under-forecasts. Confidence indicators show observation count: ●●● (n≥40), ●●○ (n≥15), ●○○ (n<15).

**Panel 3 — Key bias patterns**: The most actionable signals after five days of data.

The consistent structural patterns emerging from the data:

| Pattern | Buckets | Observed | Interpretation |
|---|---|---|---|
| Strong over-forecast | `h00_06__peak`, `h06_12__peak` | a ≈ 0.36–0.38 | AEMO over-forecasts evening peak at short horizons; calibration reduces to ~37% of raw signal |
| Flat intercept | `h12_24__peak`, `h24_48__peak` | a ≈ 0.10, 0.04 · b ≈ $90/MWh | AEMO peak forecasts beyond 12h carry near-zero information — calibration collapses to flat ~$90/MWh regardless of raw value |
| Solar over-forecast | `h00_06__solar` through `h12_24__solar` | a ≈ 0.72–0.91 | Modest compression; converges to near-passthrough at h24_48 (a=0.985) |
| Near-passthrough | `h06_12__offpeak` | a ≈ 0.88 | AEMO offpeak 6–12h forecasts are well-calibrated |
| Mild under-forecast | `h12_24__offpeak` | a ≈ 1.04 | Slight upward correction for mid-range offpeak horizons |
| Shoulder anomaly | `h06_12__shoulder`, `h12_24__shoulder` | a ≈ 1.41–1.58 | AEMO under-forecasts shoulder (20:00–22:00) persistence; n=10, provisional |

This is consistent with the academic literature — Sinclair et al. (2026) found AEMO pre-dispatch RRP contributes >60% of model importance at 2–16h horizons, and AEMO systematically over-forecasts peak prices due to conservatism bias. The flat-intercept pattern at `h24_48__peak` (a→0, b≈$90/MWh) is particularly striking: the raw AEMO 24–48h peak forecast contains essentially no predictive information and the calibration model correctly learns to ignore it.

### Warm-up period

With 3 fetches per day, expect:

| Day | Buckets active | Coverage |
|---|---|---|
| 1–3 | 0 | All passthrough |
| 4–5 | h00_06, h06_12, h12_24 | Near-term calibrated |
| 6–8 | h24_48 | 2-day horizon calibrated |
| 10–14 | h48_96, h96plus | Full 7-day calibration |

### Storage files

| File | Contents |
|---|---|
| `nem_pd7day.{region}.observation_log` | Calibration observations for the configured region |
| `nem_pd7day.{region}.calibration_coefficients` | Fitted OLS and quantile models for the configured region |
| `nem_pd7day.{region}.forecast_history` | Forecast history for the configured region |

e.g. for QLD1: `nem_pd7day.qld1.observation_log`

> **Upgrading from v2.0.3 or earlier?** Storage files are automatically migrated to the region-scoped naming on first load. Your existing observations and calibration coefficients are preserved.

To reset calibration (e.g. after changing region):

```bash
rm /config/.storage/nem_pd7day.qld1.observation_log
rm /config/.storage/nem_pd7day.qld1.calibration_coefficients
rm /config/.storage/nem_pd7day.qld1.forecast_history
```

Then reload or restart the integration.

---

## Template Sensor Examples

The `value` key in each forecast period is an alias for `calibrated` (or `raw_value` in passthrough), maintained for backward compatibility.

### Next 24-hour minimum price

```yaml
template:
  - sensor:
      - name: "PD7DAY Min Price 24h"
        unit_of_measurement: "$/kWh"
        state: >
          {{ state_attr('sensor.nem_pd7day_qld1_price_forecast', 'min_24h_value') | round(4) }}
```

### Cheapest 2-hour window start time

```yaml
template:
  - sensor:
      - name: "PD7DAY Cheapest Window Start"
        state: >
          {{ state_attr('sensor.nem_pd7day_qld1_price_forecast', 'cheapest_2h_window')['time'] }}
```

### Current calibrated price as a buy cost

```yaml
template:
  - sensor:
      - name: "QLD1 PD7DAY Buy Cost"
        unit_of_measurement: "$/kWh"
        state: >
          {% set forecast = state_attr('sensor.nem_pd7day_qld1_price_forecast', 'forecast') %}
          {% if forecast %}
            {{ forecast[0]['calibrated'] | round(4) }}
          {% else %}
            unavailable
          {% endif %}
```

---

## NEM Time Convention

All timestamps in this integration use **AEST (UTC+10:00)** with no daylight saving adjustment, matching AEMO's published data. Timestamps are always ISO-8601 with explicit `+10:00` suffix, e.g. `2026-04-14T07:30:00+10:00`.

The `nemtime` field in forecast periods is the **interval end** timestamp (AEMO convention). The `time` field is the **interval start** (`nemtime − 30 minutes`).

---

## Troubleshooting

### Integration fails to load

Check the HA log for errors from `custom_components.nem_pd7day`. The most common cause is a network issue reaching `nemweb.com.au`.

### Sensors show `unavailable`

The first fetch runs at integration load. Check **Settings → System → Logs** filtered to `nem_pd7day` for fetch errors.

### p10/p50/p90 values are `null`

Normal for the first 3–5 days. Calibration requires at least 10 observations per bucket. Check the `Calibration` sensor state for current observation count.

### h48_96 / h96plus buckets show n=0

These buckets require observations from intervals 2–4 days ahead. They begin accumulating approximately 48 hours after first install (or after upgrading from a version prior to v1.9.1 which fixed forecast history persistence).

### Recorder warnings about attribute size

Add the recorder exclusions shown in the [Configuration](#configuration) section.

---

## Version History

| Version | Changes |
|---|---|
| 2.0.7 | Calibration: passthrough when raw <= 0 (negative price regime); clamp quantile slopes to 0 |
| 2.0.6 | Fix missing await on async_added_to_hass — sensors now register correctly on HA restart |
| 2.0.5 | Live sensor state advance (30-min intervals); gas_tj fix; calibration sensor → diagnostic; forecast history merged into calibration sensor |
| 2.0.4 | Region-scoped storage keys (multi-instance safe, auto-migration); gas_tj populated in forecast history |
| 2.0.3 | Single-region enforcement — one region per integration instance, full calibration coverage |
| 2.0.2 | Clean sensor names, remove Amber dependency, add Forecast History sensor |
| 2.0.1 | Version bump post-integration |
| 2.0.0 | Multi-region sensor support, TradingIS actual prices (no Amber required), forecast persistence |
| 1.9.1 | Forecast history persistence fix — h48_96/h96plus calibration buckets now survive HA restarts |
| 1.9.0 | Comprehensive pre-deployment test suite (107 tests) |
| 1.5.0 | AEMO interval convention: `nemtime` (end) + `time` (start) on all forecast periods |
| 1.4.0 | Timezone overhaul: all timestamps ISO-8601 +10:00, UTC-safe scheduling |
| 1.3.0 | Replaced polling with `async_track_point_in_utc_time` at AEMO publish times |
| 1.2.0 | OLS + IRLS quantile calibration engine, calibration diagnostic sensor |
| 1.1.0 | CASESOLUTION (binary sensor), MARKET_SUMMARY (gas), INTERCONNECTORSOLUTION sensors |
| 1.0.0 | Initial release: PRICESOLUTION forecast sensor |

---

## License

MIT License — see [LICENSE](LICENSE) for details.
