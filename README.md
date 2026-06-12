# NEM PD7DAY Price Forecast — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-%3E%3D2024.1-blue.svg)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/github/v/release/purcell-lab/nem_pd7day)](https://github.com/purcell-lab/nem_pd7day/releases)

A Home Assistant custom integration that provides **days 2–7 electricity price and tariff forecasts** from AEMO's PD7DAY pre-dispatch data for the National Electricity Market (NEM). Designed to complement Amber Electric's 24-hour Express forecast, this integration covers the window beyond Amber's reach.

AEMO publishes PD7DAY three times per day (07:30, 13:00, 18:00 AEST). This integration fetches those updates on the same schedule and applies a two-stage on-device calibration pipeline — isotonic regression for bias correction, followed by an OLS correction using AEMO STPASA supply/demand features — to produce calibrated estimates with P10/P50/P90 confidence bands.

---

### Designed to complement Amber Express

This integration provides **days 2–7 spot price and tariff forecasts** — the window beyond Amber Electric's 24-hour Express forecast. The near-term window (next 24h) is intentionally excluded to avoid duplication.

The forecast start is dynamic:
- **12:30pm–3:30am NEM**: starts at now + 24h (rolling)
- **3:30am–12:30pm NEM**: starts at tomorrow 3:30am NEM (pinned)

Both the spot price sensor and tariff sensors apply this trim automatically.

---

## How it works — Two-stage forecasting pipeline

The integration uses a two-stage forecasting pipeline:

- **Stage 1 — PD7DAY isotonic calibration**: AEMO's 7-day PD7DAY price forecasts are calibrated using isotonic regression fit on 60 days of rolling history. This corrects systematic bias and shapes the time-of-day profile across all horizons (h0–168).
- **Stage 2 — STPASA OLS correction** (horizons h22–h120): At medium-range horizons, an OLS model trained on AEMO STPASA supply/demand features (solar UIGF, wind UIGF, surplus capacity, demand 10/50/90) further corrects the isotonic output. Beyond h120, the model falls back to isotonic-only.

### Performance vs isotonic-only

- MAE improvement at h24–168: **−10.7%** vs isotonic alone (14.65 vs 16.06 $/MWh)
- Day-rank Spearman ρ: **0.917** vs 0.850
- Solar UIGF is the dominant STPASA signal (ρ = −0.78)

---

## Features

- **Days 2–7 price forecast** — calibrated $/kWh with P10/P50/P90 confidence bands, trimmed to the window beyond Amber Express
- **Isotonic calibration** — monotone PAV regression bias correction fitted on actual TradingIS vs PD7DAY pairs, with per-bucket compression ratio, iso_mae, and P10/P90 confidence intervals
- **Interconnector flows** — interconnector MW flow forecasts for the configured region
- **Market intervention flag** — binary sensor from CASESOLUTION data
- **Calibration diagnostic** — observation count, active bucket count, fit quality per bucket
- **Live charts** — two camera entities updated after each refit:
  - **Price ToD Chart** — actual price by time of day (mean, median, P10–P90 spread across all observed intervals)
  - **Calibration Chart** — isotonic calibration goodness dashboard: compression ratio heatmap, iso_mae bars, PAV complexity scatter, and compression ratio drift time-series
- **Cloud polling** — two independent polling loops:
  - **PD7DAY** fetches at AEMO publish times: 07:30, 13:00, 18:00 AEST (3 requests/day)
  - **TradingIS** fetches actual 5-min dispatch prices every 30 minutes (48 requests/day)
- **5-minute dispatch prices** — boundary-aligned `DispatchCoordinator` polls NEMWEB TradingIS at `:01:15`, `:06:15`, ..., `:56:15` (75 s after each dispatch boundary, after NEMWEB publishes). Used as the live `native_value` for tariff and spot sensors between 30-minute PD7DAY intervals.
- **Live sensor state** — all forecast sensor states advance automatically every 30 minutes to reflect the current interval, with no fetch required
- **No third-party accounts required** — actual prices sourced directly from AEMO TradingIS
- **Dependencies** — `matplotlib`, `numpy` for chart rendering, `astral` for solar elevation (installed automatically by HACS/HA)

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

### Recorder warnings (expected, harmless)

The forecast and tariff sensors carry large attribute payloads (7 days × 48 intervals each). Home Assistant will log warnings like:

```
State attributes for sensor.nem_pd7day_*_tariff exceed maximum size of 16384 bytes.
Attributes will not be stored
```

These warnings are **expected and harmless**. The sensor `state` value (the current $/kWh price) is always recorded and available in history — only the large `forecast` attribute list is dropped by the recorder. No configuration changes are needed.

> **Do not add `recorder: exclude: entity_globs`** for nem_pd7day sensors — doing so prevents the sensor state from being recorded, which breaks the history graph in the HA UI.

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

### STPASA supply/demand outlook

The Stage 2 OLS correction is driven by AEMO's STPASA (Short-Term Projected Assessment of System Adequacy) feed, which provides the supply/demand outlook across all NEM regions in a single ZIP: solar UIGF, wind UIGF, surplus capacity, and demand 10/50/90 percentiles.

Source: [AEMO NEMWeb STPASA](https://www.nemweb.com.au/REPORTS/CURRENT/Short_Term_PASA_Reports/)

### TradingIS actual prices (every 30 minutes)

Actual NEM dispatch prices are fetched from AEMO's TradingIS reports and used to build calibration observations:

- **URL**: `https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/`
- **Schedule**: at HH:02 and HH:32 NEM time — 2 minutes after each 30-minute trading interval closes
- **Method**: downloads the 6 × 5-minute dispatch files for the closed interval, averages the RRP values ($/MWh → $/kWh)
- **Observation tagging**: each calibration observation records `actual_source: "tradingis"`

### Live dispatch prices (every 5 minutes, boundary-aligned)

5-minute dispatch prices for the sensor `native_value` are fetched from AEMO TradingIS on a boundary-aligned schedule:

- **URL**: `https://www.nemweb.com.au/Reports/Current/TradingIS_Reports/`
- **Schedule**: 75 seconds after each 5-minute UTC boundary (`:01:15`, `:06:15`, ..., `:56:15`). AEMO typically publishes 65–90 s after the interval boundary; 75 s sits within that window with margin (`_DISPATCH_POLL_DELAY_S = 75`).
- **Alignment**: uses `async_track_point_in_utc_time` with self-rescheduling one-shot callbacks — no drift from HA startup time.

### Sensor state updates (every 30 minutes)

Forecast sensor states — price forecast and interconnector flow — advance automatically at each 30-minute interval boundary (:00 and :30 past every hour). The state always reflects the current interval from the most recent fetch, without waiting for the next PD7DAY fetch. This means:

- After the 07:30 fetch, the price forecast sensor state will step through each 30-minute interval for the rest of the day
- After HA restart, the state reflects the current interval immediately on load
- Between fetches, the raw forecast data is unchanged — only the *active interval* advances

---

## Sensors

All sensors are grouped under a single HA device named **NEM PD7DAY {region}** (e.g. `NEM PD7DAY QLD1`).

### Spot Price Days 2-7

`sensor.nem_pd7day_{region}_price_forecast` — the primary calibrated spot price forecast sensor (days 2–7 only, trimmed to exclude the Amber Express window).

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
calibrated: 0.142                       # isotonic-calibrated value
p10: 0.091                             # 10th percentile (optimistic)
p50: 0.138                             # 50th percentile (median)
p90: 0.231                             # 90th percentile (conservative)
ols_mae: 0.038                         # mean absolute error of calibration fit
calibrated_source: isotonic+stpasa     # "isotonic+stpasa" (h22–h120), "isotonic", "passthrough_high", "passthrough_sanity", or "passthrough"
n_obs: 147                             # observations used for this bucket
horizon_hours: 36.5                    # hours ahead
value: 0.142                           # alias for calibrated (template compat)
```

> **Timestamp convention**: `nemtime` is the interval END timestamp as published by AEMO. `time` is the interval START (`nemtime − 30 minutes`). This matches the AEMO dispatch interval convention.

---

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
| `active_buckets` | Buckets with ≥ 20 observations (max 24) |
| `total_buckets` | 24 (6 horizons × 4 time-of-day labels) |
| `fitted_at` | ISO-8601 timestamp of last model refit |
| `summary` | Per-bucket isotonic diagnostics: n, iso_n_steps, compression_ratio, iso_mae, x_min, x_max, q10_a, q90_a |

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

### Price ToD Stats

`sensor.nem_pd7day_{region}_price_tod_stats` (Diagnostic) — current 30-minute slot's mean actual price ($/kWh), with full 48-slot statistics as attributes.

| Attribute | Description |
|---|---|
| `state` | Mean actual $/kWh for the current time-of-day slot |
| `unique_intervals` | Total unique intervals with actuals |
| `date_from` / `date_to` | Date range of recorded actuals |
| `slots` | List of 48 dicts — one per 30-min slot, each with `mean_kwh`, `median_kwh`, `p10_kwh`, `p25_kwh`, `p75_kwh`, `p90_kwh`, `n` |

---

### Source File Datetime / Data Updated

- `sensor.nem_pd7day_{region}_source_file_datetime` — timestamp of the latest AEMO PD7DAY source file
- `sensor.nem_pd7day_{region}_data_updated` — timestamp of the last coordinator data refresh

Both are diagnostic sensors (EntityCategory.DIAGNOSTIC) and do not appear on the default dashboard.

---

### PD7DAY Data / STPASA Data (diagnostic)

Two diagnostic sensors expose the full underlying forecast payloads as unrecorded HA attributes. Both are in the **Diagnostic** category and their large attribute lists are **not saved to the HA recorder/database**.

| Sensor | State | Attribute | Description |
|---|---|---|---|
| `sensor.nem_pd7day_{region}_pd7day_data` | Forecast generation time | `forecast` | Full calibrated 7-day forecast list (330 intervals). Not saved to HA recorder. |
| `sensor.nem_pd7day_{region}_stpasa_data` | STPASA run time | `intervals` | Full STPASA supply/demand interval list (288 intervals). Not saved to HA recorder. |

---

### Tariff Sensors

One sensor per (distributor, tariff_code) for the configured region. Tariff sensors cover the same **days 2–7 window** as the spot price sensor — the near-term Amber Express window is trimmed from the forecast attribute. The `native_value` (current interval tariff) is unfiltered and always returns the current price.

Supported distributors per region:

| Region | Distributors |
|---|---|
| QLD1 | Energex, Ergon |
| NSW1 | Ausgrid, Endeavour, Essential, **EvoEnergy** (serves the ACT) |
| VIC1 | Jemena, Powercor, United, AusNet, Victoria |
| SA1 | SAPN |
| TAS1 | TasNetworks |

**EvoEnergy (ACT)** — available tariff codes: `015`, `016`, `017`, `018`, `026`, `090`. The default enabled tariff is `026` (Battery Feed-in Trial), which is the export tariff.

The `native_value` is driven by the boundary-aligned `DispatchCoordinator` (5-minute live dispatch price → `spot_to_tariff()`), falling back to the current PD7DAY forecast interval if dispatch data is unavailable.

The `spot` field in each forecast entry reflects the **calibrated** spot price (same isotonic correction applied by the spot price sensor), not the raw PD7DAY value. This ensures the tariff forecast is consistent with the spot price forecast at spike intervals where `spike_credible: false`.

#### Import tariff sensors

`sensor.nem_pd7day_{region}_{network}_{tariff_code}_tariff`

| Attribute | Description |
|---|---|
| `state` | Current interval tariff price ($/kWh), incl. additional usage fee + 10% GST |
| `forecast` | Days 2–7 tariff forecast list (see entry structure below) |
| `tariff_code` | Tariff code (e.g. 6900) |
| `distributor` | Distribution network name |
| `tariff_periods` | Time-of-use period structure with rates |
| `daily_supply_charge_$` | Daily supply charge ($/day) |

Each entry in the `forecast` list contains:

| Field | Description |
|---|---|
| `time` | Interval START timestamp (`nemtime − 30 minutes`) |
| `nemtime` | Interval END timestamp (AEMO convention) |
| `spot_raw` | Uncalibrated spot price $/kWh (before bias correction) |
| `spot` | Calibrated spot price $/kWh |
| `value` | Final tariff $/kWh (import) or feed-in rate $/kWh (export) |
| `period` | Tariff period name for that interval (e.g. `peak`, `shoulder`, `off-peak`) |
| `network_rate` | Network component $/kWh for that interval |

```json
{
  "time": "2026-06-03T10:00",
  "nemtime": "2026-06-03T10:30",
  "spot_raw": 0.082341,
  "spot": 0.079812,
  "value": 0.142500,
  "period": "peak",
  "network_rate": 0.08210
}
```

#### Export tariff sensors (battery tariffs)

`sensor.nem_pd7day_{region}_{network}_{import_code}_export_tariff`

Export tariff sensors are registered for battery-eligible network tariffs where an export program exists. The `state` is the feed-in tariff ($/kWh) for the current interval, computed from the live dispatch price via `spot_to_feed_in_tariff()`. The forecast attribute gives the days 2–7 export tariff using the calibrated spot price.

Export tariff formula: `result_c_kwh / 100` (no additional usage fee, no GST — export tariffs do not include these charges).

| Network | Import tariff | Export tariff | ToD structure |
|---|---|---|---|
| Ausgrid (NSW1) | EA025 | EA029 | +3.85 c/kWh peak, −1.23 c/kWh solar sponge |
| Endeavour (NSW1) | N71 | N61 | +12.43 c/kWh peak, −1.97 c/kWh solar sponge |
| Essential (NSW1) | BLNT3AL | BLNREX2 | +11.57 c/kWh peak, −0.82 c/kWh solar sponge |
| EvoEnergy (NSW1) | 026 | 026 | Battery Feed-in Trial |
| SAPN (SA1) | RESELE | RESELE | +12.25 c/kWh peak, −1.00 c/kWh solar sponge |

#### Additional usage fee

`number.nem_pd7day_{region}_additional_usage_fee` — a configurable number entity (default `0.0293` $/kWh) added to import tariff calculations. Does **not** apply to export tariffs.

---

### Camera Entities

Three camera entities are registered on the device and can be added to any HA dashboard using a **Picture** or **Camera** card.

| Entity | Description |
|---|---|
| `camera.nem_pd7day_{region}_forecast_chart` | 7-Day Pre-Dispatch Spot Price Forecast — raw vs calibrated with P10/P90 confidence band, per-day min/max labels, spike callouts, LOR/MSL notice bands |
| `camera.nem_pd7day_{region}_price_tod_chart` | Actual price by time of day — mean, median, and P10–P90 spread across all observed intervals |
| `camera.nem_pd7day_{region}_calibration_chart` | Isotonic calibration goodness dashboard: compression ratio heatmap, iso_mae bars, PAV complexity scatter, and compression ratio drift time-series |

All charts are re-rendered after each calibration refit (07:30, 13:00, 18:00 NEM). The calibration chart reads live isotonic diagnostics so the heatmap values, n counts and confidence indicators update as calibration matures.

### 7-Day Forecast Chart

![7-Day Pre-Dispatch Spot Price Forecast](docs/forecast_chart.png)

The forecast chart shows the full 7-day ahead price window for the configured NEM region (note: the chart displays the full window for visual context, while the sensor attributes are trimmed to days 2–7):

- **Calibrated line** (blue solid) — isotonic-calibrated price forecast with P10/P90 confidence band
- **PD7DAY Raw** (grey dashed) — AEMO's raw pre-dispatch forecast before calibration
- **Daily max/min labels** — peak and trough $/kWh values annotated per day
- **AEMO Spike Forecast** (red triangle callouts) — intervals where `raw_value ≥ $3/kWh` and `spike_credible: True`; one callout per contiguous cluster pointing at the cluster peak, with the peak price labelled
- **Clip line** (red dotted) — dynamic display ceiling at p99 + 15% headroom
- **LOR/MSL notice bands** — shaded vertical regions for active NEMWEB reserve (LOR1/2/3) and minimum load (MSL1/2/3) notices, with staggered labels and a dynamic legend showing only notice types present in the window
- **Dual y-axis** — $/kWh (left) and $/MWh (right)

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
| `h00_06` — 0 to 6 hours ahead | `peak` — NEM 16:00–21:00 (hardcoded) |
| `h06_12` — 6 to 12 hours | `solar` — sun elevation > 15° and not peak |
| `h12_24` — 12 to 24 hours | `morning_ramp` — sun elevation 0°–15° and not peak |
| `h24_48` — 24 to 48 hours | `shoulder` — sun below horizon (elevation ≤ 0°) |
| `h48_96` — 48 to 96 hours | |
| `h96plus` — beyond 96 hours | |

   Time-of-day classification uses **solar elevation angle** (via the `astral` library with capital city coordinates for each NEM region) instead of fixed clock-hour boundaries. This tracks the seasonal shift in the solar generation window (~1–2 hours between summer and winter) and naturally captures the duck curve inflection points.

5. **Isotonic (PAV) fitting** — once a bucket has ≥ 20 observations, a monotone isotonic regression (Pool Adjacent Violators) is fitted using exponential time decay weighting (`weight = exp(-0.033 × days_ago)`, half-life ≈ 21 days). Separate P10 and P90 quantile slopes are also fitted. Key diagnostics per bucket:
   - `compression_ratio` — (y_max − y_min) / (x_max − x_min); values <1 indicate AEMO over-forecasts
   - `iso_mae` — mean absolute calibration shift |y_fitted − x_raw|
   - `iso_n_steps` — number of distinct steps in the fitted isotonic function (complexity)
   - `q10_a` / `q90_a` — quantile slopes driving P10/P90 confidence intervals

6. **Application** — at forecast time, each period's bucket is looked up. If active, isotonic and quantile values are returned. Otherwise the raw value passes through unchanged.

### ToD classification details

The integration classifies each 30-minute NEM interval into one of four time-of-day buckets based on solar elevation angle (using the `astral` library with the capital city coordinates for each NEM region — Brisbane, Sydney, Melbourne, Adelaide, Hobart):

| Label | Condition | Typical NEM hours |
|---|---|---|
| `peak` | NEM hour 16–21, unconditional | 16:00–21:00 |
| `solar` | elevation > 15° and not peak | ~09:00–16:00 |
| `morning_ramp` | elevation 0°–15° and not peak | ~05:00–09:00 |
| `shoulder` | elevation ≤ 0° (overnight) | ~21:00–05:00 |

**Why solar elevation instead of clock hours?**
- The solar generation window shifts by ~1–2 hours between summer and winter
- Clock-hour buckets cannot capture this seasonal drift
- Solar elevation naturally tracks the duck curve inflection points
- `morning_ramp` captures the pre-solar demand spike (gas peakers, morning load) that behaves very differently from true overnight prices

**Why morning_ramp matters:**
The 05:00–09:00 NEM window has materially different price dynamics from true overnight — demand climbs fast, gas peakers fire, and solar hasn't started yet. Previous clock-hour buckets lumped this into "offpeak", causing systematic upward bias in that window. The `morning_ramp` bucket captures this independently.

### Isotonic decay weighting

Each isotonic bucket fit uses exponential time decay weighting:
- `weight = exp(-0.033 × days_ago)` — half-life ≈ 21 days
- Observations from 21 days ago have half the influence of today's observations
- Observations from 90 days ago have ~5% influence (near zero)
- This allows the model to adapt to seasonal price dynamics, changing generation mix, and week-to-week market shifts without discarding historical data

### Bucket structure

4 ToD labels × 6 horizon bands = 24 active buckets per region. Minimum 20 observations required per bucket before it activates. New installs will see buckets activate progressively over the first few days.

### Spike Forecasts

PD7DAY raw values for days 2–7 that appear as very high prices (e.g. $8,999/MWh) are predominantly **pre-dispatch bids** placed by generators for future intervals — not genuine price spike forecasts. Generators routinely submit high bids for capacity they may or may not dispatch, especially at long horizons where the bids are speculative.

These bid-based values are filtered out of the calibrated price. The isotonic model clips them to the training-range maximum — a normal-market estimate based on observed actual prices. The integration **does not pass through raw spike values** as calibrated prices. The `calibrated` / `value` fields always reflect a normal-market isotonic estimate. This is intentional design.

The `spike_credible: True` annotation is applied to intervals where `raw_value ≥ $3/kWh` AND gas supply forecast and QNI interconnector flow conditions suggest a credible supply constraint. Even these are treated with caution at 2–7 day horizons — the annotation flags *possible* supply tightness, not a confirmed price event.

Users who want to inspect the underlying raw bid-based values can read the `raw_value` field in each forecast interval entry.

### AEMO forecast bias — QLD empirical findings

![QLD Duck Curve and AEMO Bias Analysis](docs/qld_duck_curve_bias.png)

The chart above shows three panels derived from 648 forecast vs actual observation pairs collected over the first five days of operation (Apr 15–19 2026, QLD1):

**Panel 1 — Duck curve**: The stylised QLD wholesale price profile. Actual prices follow the classic duck curve shape — a solar-driven trough around 13:00 and a sharp evening ramp as rooftop solar drops off and demand peaks around 18:30. AEMO's raw PD7DAY forecast over-estimates the evening peak height and under-corrects the solar trough depth. The calibrated forecast applies the isotonic correction to bring both in line.

**Panel 2 — Compression ratio heatmap**: Each cell shows the `compression_ratio` for that horizon × time-of-day bucket. Green (compression_ratio≈1) means AEMO's forecast is unbiased. Red (compression_ratio<1) means AEMO over-forecasts and the calibration compresses the signal. Yellow-green (compression_ratio>1) means AEMO under-forecasts. Confidence indicators show observation count: ●●● (n≥40), ●●○ (n≥15), ●○○ (n<15).

**Panel 3 — Key bias patterns**: The most actionable signals after five days of data.

### Observed actual prices by time of day

![QLD Actual Price by Time of Day](docs/qld_actual_price_tod.png)

This chart is generated from the rolling observation log and updated after each refit. Each 30-minute slot shows the mean and median actual price, with P10–P90 spread bands. The solar window (typically 10:00–15:00) frequently shows negative or near-zero prices. The bar chart below shows how many actuals have been recorded per slot — slots with fewer observations (n<10) have wider, less reliable bands.

The consistent structural patterns emerging from the data:

| Pattern | Buckets | Observed | Interpretation |
|---|---|---|---|
| Strong over-forecast | `h00_06__peak`, `h06_12__peak` | compression_ratio ≈ 0.36–0.38 | AEMO over-forecasts evening peak at short horizons; calibration reduces to ~37% of raw signal |
| Flat intercept | `h12_24__peak`, `h24_48__peak` | compression_ratio ≈ 0.10, 0.04 | AEMO peak forecasts beyond 12h carry near-zero information — calibration collapses to a near-constant value |
| Solar over-forecast | `h00_06__solar` through `h12_24__solar` | compression_ratio ≈ 0.72–0.91 | Modest compression; converges to near-passthrough at h24_48 (compression_ratio≈0.985) |
| Near-passthrough | `h06_12__offpeak` | compression_ratio ≈ 0.88 | AEMO offpeak 6–12h forecasts are well-calibrated |
| Mild under-forecast | `h12_24__offpeak` | compression_ratio ≈ 1.04 | Slight upward correction for mid-range offpeak horizons |
| Shoulder anomaly | `h06_12__shoulder`, `h12_24__shoulder` | compression_ratio ≈ 1.41–1.58 | AEMO under-forecasts shoulder (20:00–22:00) persistence; n=10, provisional |

The flat `compression_ratio` at `h96plus__peak` (cr≈0.048) confirms the raw AEMO long-range peak forecast contains essentially no predictive information — isotonic calibration correctly flattens the output to a near-constant value.

### Warm-up period

With 3 fetches per day and MIN_OBS=20, expect:

| Day | Buckets active | Coverage |
|---|---|---|
| 1–5 | 0 | All passthrough |
| 5–7 | h00_06, h06_12, h12_24 | Near-term calibrated |
| 8–10 | h24_48 | 2-day horizon calibrated |
| 12–16 | h48_96, h96plus | Full 7-day calibration |

### Storage files

| File | Contents |
|---|---|
| `nem_pd7day.{region}.observation_log` | Calibration observations for the configured region |
| `nem_pd7day.{region}.calibration_coefficients` | Fitted isotonic and quantile models for the configured region |
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

Normal for the first 5–7 days. Calibration requires at least 20 observations per bucket. Check the `Calibration` sensor state for current observation count.

### h48_96 / h96plus buckets show n=0

These buckets require observations from intervals 2–4 days ahead. They begin accumulating approximately 48 hours after first install (or after upgrading from a version prior to v1.9.1 which fixed forecast history persistence).

### Recorder warnings about attribute size

These are expected and harmless — see the [Recorder warnings](#recorder-warnings-expected-harmless) section above. No configuration changes are needed. Do not add `recorder: exclude: entity_globs` as this breaks sensor history.

### QLD1 (or any region) price forecast showing flat line / not updating

This occurs when HA's entity registry has a stale `entity_id` from a previous version registration. SA1 installs are unaffected as they were registered after the fix. To resolve:

1. Go to **Settings → Devices & Services → NEM PD7DAY [region]** device
2. Delete or disable/re-enable each affected entity
3. Restart HA — entities re-register with correct IDs

> **Note:** historical data for the affected entity will restart from the re-registration date. This is a one-time migration step.

---

## Version History

| Version | Changes |
|---|---|
| 3.0.0 | Two-stage forecasting pipeline: Stage 1 isotonic calibration + Stage 2 STPASA OLS correction (h22–h120). New STPASA client fetches all-region STPASA ZIP once per cycle and distributes to per-region stores. `fit_ols_stage2()` trains a 9-feature OLS model per bucket using STPASA supply/demand features (solar UIGF, wind UIGF, surplus capacity, demand 10/50/90). `calibrated_source` attribute is `isotonic+stpasa` at medium-range horizons, falls back to `isotonic` beyond h120. New diagnostic sensors: `<region>_pd7day_data` and `<region>_stpasa_data` expose full forecast and STPASA payloads as unrecorded HA attributes. MAE improvement −10.7% at h24–168 vs isotonic alone. |
| 2.3.57 | Intermediate release (superseded by v3.0.0) |
| 2.3.56 | Refactor: centralise STPASA download — single ZIP fetch distributes to all region stores; eliminates duplicate per-region downloads |
| 2.3.55 | Initial STPASA OLS stage2 implementation |
| 2.3.47 | EvoEnergy tariffs for NSW1/ACT (codes 015, 016, 017, 018, 026, 090; default export tariff 026 Battery Feed-in Trial); forecast entries now include `spot_raw`, `period`, `network_rate`; freshness gate accepts `settlement >= boundary`; dispatch poll delay set to 75 s (AEMO publishes 65–90 s after boundary); clarified 16kB recorder warnings are harmless |
| 2.3.40 | Fix: `DispatchCoordinator` now boundary-aligned — polls NEMWEB TradingIS at 35 s past each 5-minute UTC boundary (NEMWEB publishes ~30 s after boundary). Replaces rolling `update_interval` that drifted up to ~4 min from startup offset, causing tariff sensors to show stale dispatch prices each interval |
| 2.3.39 | Fix: tariff sensors (import + export) were passing raw PD7DAY RRP into `spot_to_tariff()` / `spot_to_feed_in_tariff()` — `store` now wired into all tariff sensor constructors; `_calibrated_value()` applies isotonic calibration identical to spot sensor; `spot` attribute in forecast lists now reflects calibrated $/kWh |
| 2.3.38 | Fix: `iso_chart.py` missing matplotlib graceful fallback — adds same `try/except ImportError` + `_placeholder_png()` pattern as `forecast_chart.py` |
| 2.3.37 | Fix: export tariff formula was including additional usage fee + 10% GST (overcounting ~4–5 c/kWh); export tariff now returns `result_c_kwh / 100` only |
| 2.3.36 | Export tariff sensors: Ausgrid EA025→EA029, Endeavour N71→N61, Essential BLNT3AL→BLNREX2, SAPN RESELE→RESELE; new SAPN RESELE import program for SA1; Day 2–7 sensors moved to `EntityCategory.DIAGNOSTIC`; `_suppress_stdout()` wraps all `aemo_to_tariff` calls to silence sapower.py debug prints |
| 2.3.14 | Fix notice pipeline: NEMWEB directory deduplication; first-run bootstrap now backfills last 7 days instead of skipping all files; upgrade-path auto-reset in coordinator resets stuck cursor when last_seen > 0 but total_notices == 0 |
| 2.3.13 | Fix AEMO market notice period regex: date follows each time token (`From HHMM hrs DD/MM/YYYY to HHMM hrs DD/MM/YYYY`); multi-period notice support; all periods merged to widest window |
| 2.3.12 | Chart improvements: per-cluster spike callouts, anti-crossover arrow placement, chart title updated to 7-Day Pre-Dispatch Spot Price Forecast, x-axis ticks at 06:00/18:00 only, MSL/LOR notice staggered labels and legend patches |
| 2.3.x | Multiple fixes: matplotlib thread-safety (OO API), Grid Notices sensor device attachment, p10/p90 None crash, blocking event loop calls, sanity check log spam |
| 2.3.0 | MSL/LOR grid stress notices: NEMWEB poller, HA .storage persistence, 7-day chart annotation bands (LOR1/2/3, MSL1/2/3), Grid Stress binary sensor, Grid Notices count sensor |
| 2.2.11 | Sanity guard tuning: SANITY_RATIO_RAW_FLOOR=0.05 $/kWh floor + SANITY_ABS_DIFF_LIMIT=0.30 $/kWh absolute backstop to prevent false positives on near-zero raw forecasts |
| 2.2.10 | Isotonic calibration: replaced weighted OLS with PAV monotone isotonic regression; per-bucket compression_ratio, iso_mae, iso_n_steps diagnostics; iso_chart camera; unconditional startup refit |
| 2.2.4 | Critical fix: spike actual prices (actual_rrp >= $3.00/kWh) were not being excluded from OLS training buckets — only the forecast passthrough path applied SPIKE_THRESHOLD. The 05 May spike event ($8,000–$15,000/MWh actuals) collapsed all medium/long-horizon peak and shoulder slopes to near zero. Fix is retrospective — call nem_pd7day.force_refit after updating to immediately restore correct coefficients. |
| 2.2.3 | Fix: adding a second NEM region failed with "This region is already configured" — config entry unique_id is now region-scoped (nem_pd7day_{region}) instead of hardcoded |
| 2.2.2 | New 7-day forecast chart camera entity per region — shows raw vs calibrated forecast with p10/p90 confidence band, per-day min/max labels ($/kWh), dual $/kWh + $/MWh y-axes, dynamic clip at p99+15% headroom, passthrough_high spike annotations; fix circular import chain through `__init__.py` (`now_nem`/`to_nem_iso` removed from `calibration_engine` imports); fix CI workflow — `astral`, `numpy`, `matplotlib` now installed in test runner; fix spike forecast callout label in $/kWh not $/MWh |
| 2.2.1 | Fix `total_buckets` sensor attribute (was hardcoded 18, now derived dynamically as `len(TOD_LABELS) × len(HORIZON_BANDS)` = 24); add `nem_pd7day.force_refit` service — triggers immediate calibration refit and coordinator refresh for one or all regions without waiting for the next scheduled fetch; fix GitHub Actions CI workflow — install `astral`, `numpy`, `matplotlib` in test runner |
| 2.2.0 | Adaptive Calibration — weighted OLS with exponential time decay (λ=0.033, half-life≈21d); solar elevation angle ToD classification via `astral` (peak/solar/morning_ramp/shoulder); 4 labels × 6 horizon bands = 24 active buckets; `astral>=2.2` dependency |
| 2.1.3 | Version bump |
| 2.1.2 | Multi-region audit: remove hardcoded `entity_id` from Gas sensor; drop Gas Generation Forecast sensor (not relevant to regional price forecasting) |
| 2.1.1 | Multi-region fix: add `name=` to camera and ToD Stats sensor `device_info` so HA generates correctly scoped entity IDs per region |
| 2.1.0 | Multi-region support: region-scoped `unique_id` and `device_info` for all entities; real ToD curves in bias chart (actual / AEMO raw / calibrated); `calibration_result` passed into `tod_stats.compute()` |
| 2.0.9 | Camera entities: Price ToD Chart (actual price by time of day) and Bias Chart (live duck curve + OLS heatmap); Price ToD Stats sensor; matplotlib + numpy dependencies |
| 2.0.8 | Fix 30-min sensor state advance (async lambda bug); spike passthrough at $3/kWh; 90-day rolling window; negative passthrough threshold -$0.10/kWh (solar trough correction) |
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
