# Spec 005: Serving gate pipeline

Status: draft, 27 September 2026, against `main` at d3023fb (v3.17.6)
Plan: docs/architecture/tech-debt-plan.md, step 005

## Responsibility

Every calibrated price the integration publishes passes through `CalibrationResult.apply` (`custom_components/nem_pd7day/calibration_engine.py:1017-1185`, 169 lines). The method decides, for one interval, whether the stage 2 STPASA correction may replace the stage 1 isotonic value, and then builds the band around whichever value it serves. Each decision was added for a different defect, and each has its own reason to change:

| Step | Lines | What it guards | Issues |
|---|---|---|---|
| Below the fitted domain | `:1028-1042` | a stage 2 prediction over a forecast the stage 1 fit never saw | #73, #114, #117 |
| Stage 2 inputs | `:1044-1052` | horizon outside `[OLS_MIN_HORIZON_H, OLS_MAX_HORIZON_H]`, or missing STPASA or run features | |
| Stage 2 model | `:1054-1058` | no OLS model for the bucket, or fewer than two coefficients | |
| Feature domain | `:1060-1093` | extrapolation beyond the uncertainty the bucket publishes | #85, #147, #153 |
| Sign agreement | `:1095-1111` | stage 2 flipping the sign stage 1 published | #73, #114 |
| Market floor | `:1112-1115` | a prediction below `MARKET_PRICE_FLOOR` | #114 |
| Stage 2 band | `:1117-1185` | the published triple: residual band or labelled fallback, containment, floor and rounding | #69, #72 |

Two related bodies sit beside it:

- **`BucketModel.apply_all`** (`:898-1003`, 104 lines) chooses one of three stage 1 paths: passthrough with no isotonic model, below the domain (#123), or isotonic unfloored above the market floor (#114, #144).
- **`CalibrationStore.apply_to_price`** (`calibration_store.py:434-495`) returns a passthrough result when nothing is fitted, calls `apply`, then adds the informational `spike_credible` annotation (#176). Spec 004 left it there for this spec.

This spec turns the decisions into an ordered pipeline of small `ServingGate` objects, each carrying the issues it guards, in a new domain-layer module `serving.py`. Adding a gate then means adding a class and one entry in a tuple, not editing a 169-line body.

`apply_all` is split into its three stage 1 paths, and the spike annotation and passthrough move into the same module. `CalibrationResult.apply`, `BucketModel.apply_all` and `CalibrationStore.apply_to_price` keep their names and signatures. The plan rates this step high risk because every published price passes through it, so it has its own release and a day of live comparison.

## Current behaviour this must preserve

Every published calibrated value, band, source label and feature key must be bit for bit the same for every input, in every branch. The pins are:

- **Golden:** every calibrated sensor in every scenario (`tests/golden/snapshots/`).
- **Existing tests:**
  - `tests/test_calibration_engine.py` and `tests/test_calibration_stage2.py`, including 29 `.apply(` and 23 `.apply_all(` calls and the gate tests for #73, #114, #117, #123, #144, #147 and #153;
  - `tests/test_calibration_store.py` (`apply_to_price`, 33 references);
  - the spike credibility tests.
- **New serving fixture (invariant 1).** It exists because the golden scenarios reach only some branches.

| Behaviour | Produced at |
|---|---|
| Stage 1 passthrough, when no isotonic model exists: point is the raw value; band ordered but not clamped; source `passthrough`; band source `stage1_raw`; feature equals raw | `:925-948` |
| Stage 1 below the domain: point is `max(edge_value, MARKET_PRICE_FLOOR)`; band is the raw band at `x_min`, clamped; source `SOURCE_ISOTONIC_BELOW_DOMAIN`; band source `stage1`, or `passthrough` when no line is fitted | `:950-971` |
| Stage 1 isotonic: `max(iso(x), MARKET_PRICE_FLOOR)`; clamped band; `ols_mae`; source `isotonic`; feature equals the published point (#144) | `:973-1003` |
| Dict key order of each stage 1 path, as built today | `:937-947`, `:961-971`, `:991-1003` |
| Gate order and exits. Each gate returns the stage 1 dict unchanged, the same object, not a copy: below domain, then inputs (`stpasa is None or run_features is None or h < OLS_MIN_HORIZON_H or h > OLS_MAX_HORIZON_H`), then model (`None or len(coef) < 2`), then feature domain (`not ols.serves(vec)`), then sign (`(pred < 0.0) != (iso < 0.0)`), then floor (`pred < MARKET_PRICE_FLOOR`) | `:1028-1115` |
| Feature vector, in order: `stage2_iso_feature(result, forecast)`, `run_max_h6_rrp`, `run_mean_rrp`, `run_spread`, `horizon_hours / 168.0`, `log_surplus`, `log_solar`, `log_demand`, `poe_spread_n` | `:1063-1075` |
| Stage 2 output: a copy of stage 1 with `calibrated = round(pred, 6)`, source `isotonic+stpasa`, `stpasa_run_at`; band from `ols.residual_band(pred)` with source `stage2`, else `bucket.raw_band(forecast)` with source `stage2_fallback`; `_clamp_band(pred, ...)`; each level rounded to 6 or None; `BAND_SOURCE_KEY` set last | `:1154-1185` |
| `apply_to_price`: with no calibration, the passthrough dict in its current key order, `calibrated` rounded to 6. Otherwise `apply`, then, when `raw_price >= SPIKE_THRESHOLD`, `spike_credible` is `bool(gas > SPIKE_GAS_THRESHOLD_TJ and network_tight)` when both are known and None otherwise; below spike territory there is no key | `calibration_store.py:445-495` |

## Interfaces

New module `custom_components/nem_pd7day/serving.py`, domain layer. It may import only `calibration_engine`'s value types and constants (by a `TYPE_CHECKING` import where needed, to avoid a cycle), `const` and the standard library.

```python
@dataclass(frozen=True)
class Stage2Context:
    forecast: float
    horizon_hours: float
    hour_of_day: int
    stage1: dict                          # the dict apply_all returned; returned unchanged on any refusal
    bucket: BucketModel
    stpasa: StpasaFeatures | None
    run_features: RunFeatures | None
    ols: OlsModel | None = None           # set by Stage2ModelGate
    features: tuple[float, ...] | None = None   # set by Stage2ModelGate, in the pinned order
    prediction: float | None = None       # set by FeatureDomainGate after serves() admits

class ServingGate(Protocol):
    name: str
    issues: tuple[str, ...]               # e.g. ("#73", "#114", "#117")
    def __call__(self, ctx: Stage2Context, models: Mapping[str, OlsModel]) -> Stage2Context | None: ...
    # None means refuse: serve the stage 1 dict unchanged.

class BelowDomainGate: ...
class Stage2InputsGate: ...
class Stage2ModelGate: ...                # looks the model up by _bucket_key and builds the feature vector
class FeatureDomainGate: ...              # ols.serves(features), then ols.predict(features)
class SignAgreementGate: ...
class MarketFloorGate: ...

SERVING_GATES: Final[tuple[ServingGate, ...]] = (
    BelowDomainGate(), Stage2InputsGate(), Stage2ModelGate(),
    FeatureDomainGate(), SignAgreementGate(), MarketFloorGate(),
)

def stage2_result(ctx: Stage2Context) -> dict: ...        # the #69/#72 band, source labels, rounding

def stage1_passthrough(bucket: BucketModel, x: float) -> dict: ...
def stage1_below_domain(bucket: BucketModel, x: float) -> dict: ...
def stage1_isotonic(bucket: BucketModel, x: float) -> dict: ...

def passthrough_result(raw_price: float) -> dict: ...      # apply_to_price with nothing fitted
def annotate_spike(cal: dict, raw_price: float, gas_forecast_tj: float | None,
                   network_tight: bool | None) -> dict: ... # mutates and returns cal, as today
```

Changes to existing code:

- **`CalibrationResult.apply`** becomes: `stage1 = bucket.apply_all(forecast)`; build the context; run each gate in `SERVING_GATES`, returning `stage1` on the first `None`; return `stage2_result(ctx)`. The method keeps its signature, and every explanatory comment moves to the gate or builder that now carries its rule.
- **`BucketModel.apply_all`** dispatches to the three stage 1 functions, in the same order of checks.
- **`CalibrationStore.apply_to_price`** becomes `passthrough_result` or `annotate_spike(self._calibration.apply(...), ...)`.
- **Names that stay importable from `calibration_engine`:** every name tests or other modules import from there, including `_clamp_band`, `_order_band`, `stage2_iso_feature`, the `BAND_SOURCE_*` and `SOURCE_*` constants, `ISO_FEATURE_KEY`, `MARKET_PRICE_FLOOR` and the `OLS_*` constants. These are defined there or imported there, never only in `serving.py`.

Amendment to the plan: the plan listed a "spike" gate and a "band containment" gate. Neither is a gate in the code. The spike check annotates and never refuses (#176 says the calibrated value is never modified by it), so it is `annotate_spike`, applied after the pipeline. Containment is `_clamp_band`, applied inside every result builder, so it stays a function every builder calls, not a stage that can be reordered.

## Invariants

1. **Bit-for-bit equality over a fixture recorded before any code moves.** On the base commit, a committed script (`scripts/record_serving_io.py`) builds several `CalibrationResult`s:
   - one fitted deterministically from synthetic observations, with stage 2 models and residual quantiles;
   - the same without residual quantiles, so the fallback band is reached;
   - one with a bucket that has no isotonic model;
   - one with no stage 2 model for some buckets;
   - no calibration at all, to reach `apply_to_price`'s passthrough.

   Against these it runs a grid of at least 20,000 inputs. The grid covers forecasts from below the domain to above `SPIKE_THRESHOLD`, including negatives, zero and the market floor; horizons either side of both OLS bounds; every hour; STPASA and run features present, absent and far outside their training ranges; and gas and network flags in every combination. It records `json.dumps` of each output dict with key order kept, plus the name of the branch that produced it.

   The fixture (`tests/fixtures/serving_io.json.gz`) and `tests/test_serving_io.py` are committed first and pass on the base commit. The test also asserts that every gate refusal, both stage 2 band paths and all three stage 1 paths are each reached at least 100 times, so no branch is pinned vacuously.
2. The golden master is identical.
3. `SERVING_GATES` holds exactly the six gates, in the order above, with the issues listed in the table. A test pins both, so reordering or dropping a gate is a visible, reviewed change.
4. A refused interval returns the stage 1 dict object itself, as today. Stage 2 returns a new dict.
5. `serving.py` imports no `homeassistant`, `calibration_store`, `coordinator` or `sensor`, and import-linter lists it in the domain layer.
6. The number of `ols.serves` and `ols.predict` calls per `apply` call is unchanged: at most one of each, and none after an earlier refusal. A counting test pins this, because the serving path runs about 1,650 times per state write.

## Migration

Each step leaves the suite, the golden master and the serving fixture green.

1. On the base commit, add `scripts/record_serving_io.py`, the fixture it writes and `tests/test_serving_io.py`. Commit them alone, passing.
2. Add `serving.py` with the stage 1 functions, and point `apply_all` at them.
3. Add the context, the six gates, `SERVING_GATES` and `stage2_result`, and point `CalibrationResult.apply` at them.
4. Add `passthrough_result` and `annotate_spike`, and point `apply_to_price` at them.
5. Add the contract tests (invariants 3, 4 and 6), update `.importlinter`, tighten `scripts/size_baseline.json` with `--update`, and lower `mypy_baseline.txt` if it moved.

No existing test file is edited. If a step seems to need an edit to an existing test, stop and report it; do not make the edit.

## Non-goals

- Any change to what a gate decides, including thresholds, constants, comparisons and order. Findings go to issues.
- The fitting side: `CalibrationEngine.fit` and `fit_ols_stage2`, which are spec 006. Also `OlsModel.serves`, `residual_band` and the extrapolation cost, which the gates call unchanged.
- `camera.py`'s use of the spike gate result for chart callouts. It reads the same annotation and is unchanged.
- `summary`, `get_iso_diagnostics` and storage.

## Acceptance

- [ ] The serving fixture test passes on the base commit (evidence: its commit, before any code moves) and on the head, with every branch reached at least 100 times.
- [ ] Golden master identical, with no file under `tests/golden/snapshots/` changed.
- [ ] Full suite passes on 3.13 and 3.11. `git diff origin/main -- tests` adds files only.
- [ ] Coverage gate passes.
- [ ] mypy is no higher than 36, with zero errors in `serving.py`.
- [ ] Import contracts: `serving` is in the domain layer, and there is no new violation.
- [ ] Size: `calibration_engine:CalibrationResult.apply` (169) and `calibration_engine:BucketModel.apply_all` (104) leave the function baseline, and `CalibrationResult` shrinks. `serving.py` has no function over 60 lines and no class over 250 lines or 15 methods.
- [ ] A benchmark over the fixture grid shows `apply` no more than 10 per cent slower than on the base commit. The golden-master run time is reported alongside it.
- [ ] PR title starts with `refactor:`.

## Release

This change ships alone, as the plan requires for this step. Before the deploy, all calibrated sensors are dumped. After it, the deploy is compared as for v3.17.4 to v3.17.6, and a second comparison 24 hours later covers the day's refits.

## Rollback

The change is a single squash commit with no stored data. Revert it and release.
