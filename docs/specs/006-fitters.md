# Spec 006: Fitters

Status: approved 29 September 2026 with option 1; implemented (this PR); drafted against `main` at c756c3e (v3.19.0)
Plan: docs/architecture/tech-debt-plan.md, step 006

## Responsibility

Every calibration the integration serves is fitted by two methods on `CalibrationEngine` (`custom_components/nem_pd7day/calibration_engine.py`):

- **`fit`** (`:1334-1490`, 157 lines) fits stage 1: one isotonic model, one weighted least-squares line and three quantile lines per bucket.
- **`fit_ols_stage2`** (`:1492-1715`, 224 lines) fits stage 2: one nine-feature least-squares model per bucket, with a leverage screen, residual quantiles and feature ranges.

Both are on the function size baseline, and together they make `CalibrationEngine` a 498-line class. Each body mixes several steps, and each step changes for its own reason:

| Step | Lines | What it does | Issues |
|---|---|---|---|
| Rolling window | `:1356-1372` | keeps observations whose interval is at or after `now − OBSERVATION_WINDOW_DAYS`; a naive timestamp is read as NEM time, and an unparseable one is kept and weighted as of `now` | #109 |
| Stage 1 partition | `:1374-1402` | drops interventions and spikes on either side; keys by solar time of day; caps each bucket at `MAX_OBS`; computes the decay weight | #208, #209 |
| Stage 1 bucket fit | `:1404-1471` | least-squares line with its slope clamped at zero; metrics and the isotonic model from `MIN_OBS` rows; three quantile lines whose slopes are sorted, then clamped | #103, #110 |
| Stage 1 result | `:1472-1490` | recounts the training rows, logs, and builds the `CalibrationResult` | |
| Stage 2 inputs | `:1518-1523` | run features from every observation; a second stage 1 fit on the wall clock | #210 |
| Stage 2 rows | `:1530-1591` | filters, the join to STPASA and run features, clock-hour keys, the below-domain exclusion, and the feature vector | #68, #79, #85, #117 |
| Stage 2 bucket fit | `:1593-1671` | the `OLS_MIN_OBS` floor, `lstsq`, one leverage screen and refit, R², residual quantiles, and feature ranges rounded outward | #72, #79, #117, #147 |
| Stage 2 logs | `:1673-1715` | a warning for buckets without residual quantiles, and one summary line | #72, #79 |

This spec moves both fits into a new domain-layer module, `fitting.py`:
- `Stage1Fitter` and `Stage2Fitter`, one small function per step above;
- explicit inputs, so a stage 2 fit is given its stage 1 result and run features rather than fitting and computing them itself;
- `CalibrationEngine.fit` and `fit_ols_stage2` keep their names, signatures and results, and delegate.

The plan rates this step high risk because it is numerical and requires exact equality. So it has a fixture recorded before any code moves, its own release, and a day of live comparison.

## Current behaviour this must preserve

Every fitted number, every stored field and every log line must be the same, bit for bit, for every input. That includes values that look wrong, which are filed as issues and pinned here as they are (#208, #209, #210). The pins are:

- **Golden:** the calibrated sensors in every scenario that fits. `ObservationSeed` runs the real `CalibrationStore.async_refit`, and `prefit=True` runs it twice.
- **Existing tests:**
  - `tests/test_calibration_engine.py` (21 fit calls);
  - `tests/test_calibration_stage2.py` (23);
  - `tests/test_calibration_store.py` (8);
  - the stage 2 exclusion, leverage, residual band, floored feature and feature domain tests, and the camera and tariff parity tests that fit first.
- **New fit fixture (invariant 1).** It exists because the golden scenarios reach only some branches.

| Behaviour | Produced at |
|---|---|
| `now_utc = now or datetime.now(timezone.utc)`; `now_nem_dt` in `NEM_TZ`; `fitted_at` is `now_nem_dt.isoformat()` | `:1356-1357`, `:1404` |
| Window: `obs_dt >= cutoff`; naive → `NEM_TZ`; `ValueError` or `TypeError` → kept, with `now_nem_dt` as its time; `observations_in_window` counts every kept row, interventions and spikes included | `:1359-1372` |
| Partition: all 24 keys from `all_bucket_keys()` pre-created in that order; skip `is_intervention`; skip `actual_rrp >= SPIKE_THRESHOLD or pd7day_forecast >= SPIKE_THRESHOLD`; key `_bucket_key_solar(horizon, obs_dt in NEM_TZ, region)`; append only while `len < MAX_OBS` (the first rows in input order win, #209); weight `exp(-DECAY_LAMBDA * max(days_ago, 0.0))`, with `days_ago` taken from `now_nem_dt` | `:1374-1402` |
| Bucket fit, in `all_bucket_keys()` order, one `BucketModel` per key even when empty: `_ols(pairs, weights or None)`, `a = max(a, 0.0)`; `mae` and `rmse` only from `MIN_OBS` rows; `LinearCoeff(a, b, n, mae, rmse)`; isotonic from `MIN_OBS` rows with the weights (or ones); per `QUANTILES` `_quantile_regression(pairs, q, weights or None)`; the three slopes sorted ascending and given to q10, q50 and q90 in that order, each clamped at 0, each quantile keeping its own intercept and loss; `pinball_loss` only from `MIN_OBS` rows | `:1406-1463` |
| Per-bucket debug line from `MIN_OBS` rows: text, argument order, `mae or 0` | `:1464-1471` |
| `total_observations`: a recount of the kept rows that are not interventions and not spikes (before the `MAX_OBS` cap, so it can exceed the fitted rows); info line "Calibration fit complete…" with `len(observations)` and the count of buckets whose `ols` is not default | `:1472-1484` |
| Stage 2: `_compute_run_features(observations)` first, then `self.fit(observations, region=region)` with no `now`, so a wall-clock refit (#210) | `:1518-1523` |
| Stage 2 rows, filters in this order: intervention; horizon outside `[OLS_MIN_HORIZON_H, OLS_MAX_HORIZON_H]`; spike on either side; no STPASA features for `f"{interval_time}|{forecast_run_at}"`; no run features for the run. Key is clock-hour `_bucket_key(horizon, hour_of_day)`; below-domain rows are counted per key and dropped; the feature is `stage2_iso_feature(bucket.apply_all(x), x)`; the vector is in the `STAGE2_FEATURE_NAMES` order; no rolling window (#210) | `:1530-1591` |
| Stage 2 fit, over `sorted(set(rows) \| set(excluded))`: fewer than `OLS_MIN_OBS` rows → `OlsModel(bucket_key)`; `X` has a leading column of ones, `float` dtype; `lstsq(rcond=None)`; `LinAlgError` → empty; leverage `> STAGE2_LEVERAGE_MULTIPLE * p / n` screened once, the count recorded even when the bucket then falls back; refit; R² is 0 when `ss_tot <= 1e-12`; `coef` rounded to 8, `r2` to 6; `resid = _residual_quantiles(key, X, y, coef)`; ranges from the screened `X[:, 1:]`, floor and ceil at 1e-6 | `:1593-1671` |
| Stage 2 warning (sorted keys) when a bucket has coefficients but no fitted residuals; info line "OLS stage2 fit: … buckets evaluated …" with `_per_bucket_counts` for exclusions and screening; the result dict in sorted key order | `:1673-1715` |
| Logger: every line above is emitted on `custom_components.nem_pd7day.calibration_engine`, at the level and with the format string and arguments shown, so a `logger:` entry a user has set for that module keeps catching them | `:247` |
| The two executor jobs in `CalibrationStore.async_refit` call `self._engine.fit(obs, region)` and `self._engine.fit_ols_stage2(obs, map, region)`, unchanged | `calibration_store.py:354-367` |

## Interfaces

New module `custom_components/nem_pd7day/fitting.py`, domain layer. It imports:
- from `.calibration_engine`: the value types and helpers it uses (`Observation`, `StpasaFeatures`, `RunFeatures`, `BucketModel`, `LinearCoeff`, `QuantileCoeff`, `OlsModel`, `CalibrationResult`, `IsotonicRegression`, `_ols`, `_ols_metrics`, `_quantile_regression`, `_hat_leverage`, `_residual_quantiles`, `_per_bucket_counts`, `_bucket_key`, `_bucket_key_solar`, `all_bucket_keys`, `stage2_iso_feature`, and the constants);
- `.const`, `numpy`, and the standard library.

```python
Pair = tuple[float, float]                       # (pd7day_forecast, actual_rrp)
Stage2Row = tuple[list[float], float]            # (feature vector without intercept, actual_rrp)

def window_observations(
    observations: Sequence[Observation], now_utc: datetime,
) -> list[tuple[Observation, datetime]]: ...

def partition_stage1(
    windowed: Sequence[tuple[Observation, datetime]], region: str, now_nem: datetime,
) -> tuple[dict[str, list[Pair]], dict[str, list[float]]]: ...   # pairs and weights per key

def fit_quantile_lines(
    pairs: list[Pair], weights: list[float] | None,
) -> tuple[QuantileCoeff, QuantileCoeff, QuantileCoeff]: ...     # sorted slopes, clamped

def fit_stage1_bucket(key: str, pairs: list[Pair], weights: list[float]) -> BucketModel: ...

def training_count(windowed: Sequence[tuple[Observation, datetime]]) -> int: ...

class Stage1Fitter:
    def fit(
        self, observations: Sequence[Observation], region: str = "QLD1",
        now: datetime | None = None,
    ) -> CalibrationResult: ...

@dataclass
class Stage2Rows:
    rows: dict[str, list[Stage2Row]]
    excluded: dict[str, int]                     # below the stage 1 domain, per key

def stage2_rows(
    observations: Sequence[Observation],
    stpasa_by_key: Mapping[str, StpasaFeatures],
    run_features: Mapping[str, RunFeatures],
    stage1: CalibrationResult,
) -> Stage2Rows: ...

def fit_stage2_bucket(bucket_key: str, rows: list[Stage2Row]) -> tuple[OlsModel, int]: ...
    # the model, and the rows the leverage screen removed (0 when none)

def log_stage2_fit(
    models: Mapping[str, OlsModel], excluded: Mapping[str, int], screened: Mapping[str, int],
) -> None: ...

class Stage2Fitter:
    def fit(
        self,
        observations: Sequence[Observation],
        stpasa_by_key: Mapping[str, StpasaFeatures],
        stage1: CalibrationResult,
        run_features: Mapping[str, RunFeatures],
    ) -> dict[str, OlsModel]: ...
```

Changes to existing code:

- **`CalibrationEngine.fit(observations, region="QLD1", now=None)`** returns `Stage1Fitter().fit(observations, region, now)`.
- **`CalibrationEngine.fit_ols_stage2(observations, stpasa_by_key, region="QLD1")`** does three things, in today's order:
  1. computes `run_features = _compute_run_features(observations)`;
  2. computes `stage1 = self.fit(observations, region=region)`, still with no `now` (#210 pinned);
  3. returns `Stage2Fitter().fit(observations, stpasa_by_key, stage1, run_features)`.
- **Docstrings and comments move with their rule.** Every "WHY" comment and issue reference in the two bodies moves to the function that now carries that rule; none is dropped.
- **Names stay importable from `calibration_engine`.** Every name tests or other modules import from there stays defined there. No helper moves out of the engine in this spec.
- **Logger.** `fitting.py` logs through `logging.getLogger(f"{__package__}.calibration_engine")`, not its own module name (see the logger row above).

### Design choice: the engine reaches the fitters by a function-level import

`fitting.py` needs the engine's value types, and `CalibrationEngine` has to call the fitters. A module-level import in both directions is a cycle. Both the golden harness (`tests/golden/harness.py`, `_import_order`, which raises on a cycle among module-level relative imports) and Python itself would reject it. There are three ways out:

1. **Chosen:** `CalibrationEngine.fit` and `fit_ols_stage2` import `fitting` inside the method body. It is two lines, the harness does not see a function-level import, and it runs a few times a day, never on the serving path. A comment at each import names this spec and the cycle.
2. Move the value types (`IsotonicRegression` to `CalibrationResult` and the bucket keys, about 800 lines) into a lower `calibration_model.py`. Both the engine and `fitting` would then import it, and there would be no cycle. That is a far larger move than this step, and it touches every importer of those names. It is a candidate for a later spec, which would also remove the function-level import.
3. Keep `Stage1Fitter` and `Stage2Fitter` inside `calibration_engine.py`. There would be no cycle, but the fit would stay in a 1,800-line module, and the plan's aim, "a testable fit without the engine", would be only half met.

Approved with option 1.

### As implemented

The move, the interfaces and the engine's delegation are as specified. These points differ from the text above, each for the reason given.

- **`fitting.py` imports `.serving` too.** `SPIKE_THRESHOLD`, `OLS_MIN_HORIZON_H`, `OLS_MAX_HORIZON_H` and `stage2_iso_feature` are defined there (spec 005). Each name is imported from the module that defines it, not through the engine's re-export. The invariant 6 test pins the package imports to `calibration_engine`, `const` and `serving`.
- **Four private helpers beyond the listed interface**, to keep every function under 60 lines:
  - `_stage2_joined`: the row filters and the two joins;
  - `_stage2_row`: the below-domain check and the feature vector;
  - `_lstsq`: `lstsq` or None on `LinAlgError`;
  - `_ols_model`: R², residual quantiles and ranges.
- **The engine keeps every name it imported.** The constants and serving names that only the fit used are now explicit re-exports (`X as X`), with a comment, so every `from calibration_engine import …` keeps working. Only the engine's own unused `timedelta` import is dropped.
- **One comment corrected, not just moved.** The below-domain comment said the exclusion "excludes nothing unless a row's forecast changed after the stage-1 fit". It now says a row lands there when its clock-hour bucket's domain, fitted from solar-keyed rows, does not cover it (#208). The fixture's main case excludes 69 rows that way.
- **Fixture.** 13 cases, 20,610 observations, 665 KB gzipped; a run takes about 5 s. Every required branch is reached. `resid_missing` is not, as expected: `_residual_quantiles` always gets at least `OLS_MIN_OBS` finite rows.
  - **Clock:** the recorder freezes the clock by rebinding `datetime` in every loaded package module that holds the real class, rather than with `FrozenClock`. `FrozenClock` requires every clock-reading module in the package to be loaded, HA stubs included. The rebinding follows the clock read into `fitting.py` without knowing it moved.
  - **Non-finite feature:** it is `inf`, not NaN. NaN is unequal to itself, which would break the exact comparison after a JSON round trip. LAPACK rejects `inf` the same way, so the `lstsq` error path is still reached.
- **Contract tests** (`tests/test_fitting.py`, 9 tests):
  - invariant 3: one stage 1 fit per stage 2 fit, with `now` None; `Stage2Fitter` never fits stage 1;
  - invariant 4: equal models for equal arguments; a hand-built stage 1 result decides the exclusion; the screened count is kept when a bucket falls back;
  - invariant 6: imports, the function-level engine import, and the logger name.
- **Benchmark:** `fit` then `fit_ols_stage2` on the fixture's main case (9,517 observations), Python 3.13, best of 20, base and head alternating: 494.0 ms on 53c7e18 and 494.2 ms on the head. The golden master (19 tests) ran in 20.1 s on the base and 19.5 s on the head.
- **Gates:**
  - mypy stays at 35, with no errors in `fitting.py` (the moved code had none before either);
  - the size baseline goes from 30 functions and 5 classes to 28 and 4;
  - import-linter reports the same violations as `main`, none of them in `fitting`.

## Invariants

1. **Bit-for-bit equality over a fixture recorded before any code moves.** On the base commit, a committed script, `scripts/record_fit_io.py`, generates observation sets from a seeded `random.Random` and writes the inputs into the fixture itself. The fixture therefore does not depend on the generator staying stable.

   Every case runs under a `tests/golden/clock.FrozenClock` at a fixed instant. That freezes both `fit`'s default `now` and the wall-clock refit inside `fit_ols_stage2`, wherever the clock read lives after the move.

   For each case it calls `CalibrationEngine().fit(...)` (with and without an explicit `now`) and `fit_ols_stage2(...)`, then records:
   - `json.dumps(engine.to_storage(result))`, key order kept, with the stage 2 models attached as `async_refit` attaches them;
   - every bucket's isotonic breakpoints, as `float.hex` of each x and y (`to_storage` does not carry them);
   - every log record from `custom_components.nem_pd7day.calibration_engine` at DEBUG and above: level, format string and `repr` of the arguments.

   The cases cover, at the least:
   - every region in `REGION_COORDS` and one unknown region;
   - 100 days of history across the 90-day cutoff, with rows exactly on it;
   - naive, aware and unparseable timestamps;
   - interventions;
   - spikes in the forecast only, the actual only, and both;
   - a bucket over `MAX_OBS`;
   - buckets of 0, 1, `MIN_OBS − 1`, `MIN_OBS` and 11 rows;
   - constant forecasts (the `_ols` fallback);
   - a falling relationship (the slope clamps);
   - quantile slopes that arrive out of order;
   - for stage 2:
     - no STPASA features, and no run features, for some rows;
     - both horizon bounds, and rows just outside them;
     - morning rows that stage 1 keys as `morning_ramp` and stage 2 as `shoulder`, which reach the below-domain exclusion (#208);
     - an isolated high-leverage row;
     - a bucket that the screen takes below `OLS_MIN_OBS`;
     - constant actuals (R² of 0);
     - a non-finite feature that makes `lstsq` raise;
     - exactly `OLS_MIN_OBS` rows.

   The fixture (`tests/fixtures/fit_io.json.gz`, under 2 MB) and `tests/test_fit_io.py` are committed first and pass on the base commit.

   The test asserts that each listed branch is reached, counted by replaying its condition in the recorder. The recorder prints any branch the base cannot reach (for example, the residual-quantile warning, since `_residual_quantiles` is only ever given at least `OLS_MIN_OBS` rows). The PR lists those branches with the reason; they are not forced by editing code.

   The test carries the golden master's `requires_py312_sum` reason. `_ols`, `_ols_metrics` and `_compute_run_features` sum floats with the builtin `sum()`, which is compensated from Python 3.12, and the fixture is recorded on 3.13, the version CI and Home Assistant run.
2. The golden master is identical.
3. **One stage 1 fit inside a stage 2 fit, as today.** A counting test wraps `Stage1Fitter.fit` and pins the counts:
   - `fit_ols_stage2` calls it exactly once, with no `now`;
   - `Stage2Fitter.fit` never calls it.
4. **`Stage2Fitter.fit` is a function of its arguments.** Given the same observations, map, stage 1 result and run features, it returns equal models. Given a hand-built stage 1 result, it keys the below-domain exclusion by that result's buckets, which is the testability the plan asks for. A contract test builds one.
5. **Log lines are unchanged.** For each fixture case the record list equals the base's: logger name, level, format string and arguments. This is part of invariant 1, and stated here so it is not read as incidental.
6. **Layering.**
   - `fitting.py` imports no `homeassistant`, `calibration_store`, `refit_service`, `coordinator` or `sensor`, and `.importlinter` lists it in the domain layer.
   - A contract test pins its package imports to `.calibration_engine` and `.const`.
   - `calibration_engine.py` has no module-level import of `fitting`.

## Migration

Each step leaves the suite, the golden master and the fit fixture green.

1. On the base commit, add `scripts/record_fit_io.py`, the fixture it writes and `tests/test_fit_io.py`. Commit them alone, passing.
2. Add `fitting.py` with the stage 1 functions and `Stage1Fitter`, and point `CalibrationEngine.fit` at it.
3. Add the stage 2 functions and `Stage2Fitter`, and point `fit_ols_stage2` at them.
4. Add the contract tests for invariants 3, 4 and 6, and add `fitting` to the domain layer in `.importlinter`.
5. Tighten `scripts/size_baseline.json` with `--update`, and lower `mypy_baseline.txt` if it moved.

No existing test file is edited, and no patch or import path needs retargeting: no test patches a name inside either fit. If a step seems to need an edit to an existing test, stop and report it; do not make the edit.

## Non-goals

- **Any change to what a fit computes.** That includes the three defects found while writing this spec, each pinned here as it is and left to its own PR with a deliberate golden update:
  - #208: stage 1 trains on solar time-of-day buckets but serving looks up clock-hour ones, so the `morning_ramp` buckets are fitted and never served;
  - #209: `MAX_OBS` keeps the oldest rows of a full bucket;
  - #210: stage 2 refits stage 1 on the wall clock, and trains on rows outside the window.
- **Moving the numerical helpers out of the engine:** `_ols`, `_ols_metrics`, `_quantile_regression` (86 lines, on the size baseline), `_loo_residuals`, `_hat_leverage`, `_conformal_index`, `_residual_quantiles`, `_compute_run_features`, `IsotonicRegression` and `_pav`. Tests import several of them from the engine, and moving them waits on option 2 above.
- **The rest of the engine and the store:**
  - `to_storage` and `from_storage`, `CalibrationResult.summary` (85 lines) and `OlsModel.residual_band` (the engine's one mypy error, `:628`);
  - `CalibrationStore.async_refit`: its awaits, the fit generation and the STPASA feature map. The map moves to `RunFeaturesProvider` in spec 007.

## Acceptance

- [ ] The fit fixture test passes on the base commit (evidence: its commit, before any code moves) and on the head. Every listed branch is reached, or appears in the PR's list of unreachable branches with a reason.
- [ ] Golden master identical, with no file under `tests/golden/snapshots/` changed.
- [ ] Full suite passes on 3.13. `git diff origin/main -- tests` adds files only.
- [ ] Coverage gate passes.
- [ ] mypy is no higher than 35, with zero errors in `fitting.py`.
- [ ] Import contracts: `fitting` is in the domain layer, and there is no new violation.
- [ ] Size:
  - `calibration_engine:CalibrationEngine.fit` (157) and `fit_ols_stage2` (224) leave the function baseline;
  - `calibration_engine:CalibrationEngine` (498 lines) leaves the class baseline;
  - `fitting.py` has no function over 60 lines and no class over 250 lines or 15 methods.
- [ ] Benchmark: a full refit (`fit` then `fit_ols_stage2`) on the largest fixture case, best of 20, base and head alternating, is no more than 10 per cent slower on the head. The golden-master run time is reported alongside it.
- [ ] No method shared by assignment introduced.
- [ ] PR title starts with `refactor:`.

## Release

This change ships alone, as the plan requires for this step.

The golden recorded tier (spec 000 Part B) is not built, so the live install cannot replay the same observations through both releases. The live check therefore rests on the fixture and the golden master for equality, and on consistency for the deploy:
- dump the 59 sensors before and after, as for v3.17.4 to v3.19.0;
- compare the calibration sensors' `summary` after the first post-deploy refit with a refit of the same stored observations on the previous release. This is possible only if the observation store can be exported from the install; if it cannot, say so in the release report rather than implying it was done;
- a second dump 24 hours later covers the day's refits.

## Rollback

The change is a single squash commit with no stored data: the coefficient file's format is untouched. Revert it and release.
