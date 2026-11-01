# Forecast timeline: sources, hub, timeline, entities

Status: as built, 1 November 2026, at the end of the forecast timeline plan (#333, stages 0 to 6; v3.40.1 to v3.49.0). Pre-dispatch calibration (stage 3b, #337) is the one part still to come, and has a single seam waiting for it (`predispatch_calibration`).

Every price a sensor publishes for an interval comes from one place: the region's **price timeline**. Each NEMWEB source is fetched once for the install, worked into series once per run in a shared **forecast hub**, merged into one timeline per region, and published by each entity as a view of that timeline.

```
 NEMWEB sources (one fetch each per install)          domain / service modules
 ──────────────────────────────────────────           ────────────────────────
 DispatchIS / ELEC_NEM_SUMMARY ─ DispatchCoordinator (5 min) ──┐
 P5MIN ──────────────────────── P5MinCoordinator (5 min, after dispatch) ──┤
 PredispatchIS ──────────────── PredispatchCoordinator (30 min) ──┤  local_price, predispatch_local_price,
 TradingIS ──────────────────── ActualPriceService (:02/:32, all entries) │  zones: per-run series
 PD7DAY + STPASA ────────────── PD7DayCoordinator per region ──┐  │
                                     │                          │  │
                                     ▼                          │  ▼
                    CalibratedForecast memo (per region)        │  ForecastHub (hass.data, one per install)
                    warmed off the loop by the forecast         │   ├─ p5 / pd series per (run, args, first end ahead, stale)
                    sensors; tariffs and the chart wait         │   └─ keep(): timelines, forecast entries, summaries,
                    for its publish (wait_current)              │      pre-dispatch constraint rows
                                     │                          │
                                     ▼                          ▼
                    price_timeline.build_timeline(now, region, dispatch, p5, pd, pd7day)
                      dispatch (actual) → P5MIN (raw, 5 min) → pre-dispatch (served by
                      predispatch_calibration: passthrough until 3b) → PD7DAY (calibrated)
                      a stale source falls through; a half hour is split where a source ends
                                     │
                    region_timeline.region_timeline(): kept in the hub per source version
                    and 5-minute interval; timeline_forecast.forecast_entries(): kept per timeline
                                     │
   ┌───────────────┬─────────────────┼────────────────┬──────────────────┬───────────────────┐
   ▼               ▼                 ▼                ▼                  ▼                   ▼
 Spot forecast   Day 2-7          Tariffs          Forecast chart     Scarcity premium    Price Timeline
 forecast,       forecast from    tariff_forecast: camera: memo +     half_hour_values    sensor (diagnostic)
 min/max,        the Amber        priced per       timeline, entries   of the timeline
 cheapest 2 h    cutoff           interval, kept   on the loop,
 by time                          per run + fee    render off it
```

The local sensors (DUID, node, zone) and the pre-dispatch sensors read the same shared coordinators and hub for their own series: local price adjustments rather than the region price, so they are not views of the timeline itself. Their pre-dispatch entries carry `calibrated_rrp_mwh` from the same `predispatch_calibration` function the timeline serves pre-dispatch through.

## Modules, by layer

The layers are enforced by import-linter in CI (`.importlinter`; `tests/test_import_layers.py` keeps every module in one).

| Layer | Timeline modules | Role |
|---|---|---|
| Domain | `price_timeline`, `timeline_forecast`, `tariff_forecast`, `predispatch_calibration`, `local_price`, `predispatch_local_price`, `zones` | Pure functions of their inputs: no Home Assistant, no clock (`now` is passed in) |
| Services | `forecast_hub`, `calibrated_forecast`, `calibration_inputs`, the NEMWEB clients | Memo and protocol: the hub's keys, the calibrated forecast memo, its warm and wait |
| Adapters | `region_timeline`, the coordinators, `sensor`, `tariff_sensor`, `camera`, `scarcity_sensor`, `node_price_sensor`, `zone_sensor`, `predispatch_sensor` | Read `hass.data` and the entry, write state |

## The rules that keep it cheap

- **One fetch per source per install.** The P5MIN, pre-dispatch and constraints coordinators are shared through `hass.data`. TradingIS is fetched once per half hour for every entry (`ActualPriceService`), down from once per entry.
- **One build per version.** The hub keys each series on the run's identity and the clock's part of the answer, the first interval end still ahead and staleness, never on `now` itself. `keep()` holds derived values (timelines, forecast entries, constraint rows) while the identities they name hold, pinning those objects so an `id` is never reused.
- **No calibration on the event loop when a warm is coming.** The forecast sensors warm the calibrated memo in the executor before writing. The tariffs and the chart, which read the memo but do not build it, wait for its publish (`CalibratedForecast.wait_current`, up to `MEMO_WAIT_SECONDS`) instead of calibrating themselves. Only past that wait (every forecast sensor of the region disabled) or on a tariff's very first write at startup does a reader calibrate for itself.
- **Writes once per source version.** P5MIN and pre-dispatch coordinators notify only on a new run (`always_update=False`). The spot sensor writes on each dispatch poll and each new run; the tariffs on each dispatch poll and half hour, through the wait.

## Where stage 3b plugs in

`predispatch_calibration.calibrated_rrp_mwh(region, end, rrp)` and `calibrated_source(region, end)` are the only places pre-dispatch is served. Stage 3b replaces their bodies with the calibration fitted on the observations stage 3a records (`PredispatchRecord`), and the timeline, every view of it, the node and zone `calibrated_rrp_mwh` and the constraints sensor's `calibrated_rrp` follow. The node sensor's hub key notes that the calibration's identity must join it then.

## Measured

Final benchmark against the stage 0 baseline, on #333. Gates at the end of stage 6:

| Gate | Start of stage 6 (v3.48.0) | End (v3.49.0) |
|---|---|---|
| mypy | 34 errors counted in CI, 57 with the chart libraries typed | 0, with them typed; CI fails on any |
| import-linter | report only, 2 contracts broken | enforced, 2 kept, every module in a layer |
| size baseline | 28 functions, 4 classes over the limits | 27 functions, 3 classes, seven entries smaller |
