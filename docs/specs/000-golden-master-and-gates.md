# Spec 000: Golden master and refactor gates

Status: draft
Plan: docs/architecture/tech-debt-plan.md, step 000
Measured on: `main` at `1179345` (v3.17.0 plus #169)
Depends on: the housekeeping PR in plan section 4 (dead spike constants removed, stale `camera._build_forecast_data` docstring rewritten, `mypy_baseline.txt` at 59), so the first snapshots and baselines are taken after it

## Responsibility

Build the safety net every later spec is accepted against, and nothing else. Three parts:

- **A. Golden master.** A test that runs the real platform setup code over fixed inputs with a frozen clock, and compares every entity's observable output to a committed snapshot, exactly.
- **B. Recorded inputs.** An opt-in, read-only service that exports one config entry's persisted inputs from the live install, so the golden master can include real runs as well as synthetic ones.
- **C. Gate tooling.** The coverage comparison, the size ratchet, the import contract, the assertion guard and the per-module mypy counts, wired into CI.

Part A can land alone; B and C follow in the same PR or the next one. No spec from 001 onwards starts until all three are merged.

The golden master starts from parsed inputs, so it does not cover the NEMWEB parsers. That net belongs to spec 010, which pins parser output on committed sample files before it moves anything.

## Current behaviour this must preserve

Spec 000 changes no published value. Parts A and C are test and CI files only. Part B adds one service and touches nothing else.

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Every entity's state, attributes, name, unique id, enabled default | `sensor.py`, `tariff_sensor.py`, `scarcity_sensor.py`, `binary_sensor.py`, `number.py`, `camera.py` | the full suite today; the golden master after this spec |
| The four platforms and nothing else | `__init__.py:57` `PLATFORMS` | `tests/test_lifecycle.py` |
| `force_refit` is the only registered service | `__init__.py:550` | `tests/test_lifecycle.py`; Part B adds a second, asserted by a new test |
| The scarcity premium sensor exists for QLD1 only | `sensor.py:147` | `tests/test_scarcity_sensor.py`; every QLD1 scenario after this spec |
| Spike credibility has two published views: the sensor attribute suppressed inside `SPIKE_COVARIATE_MIN_HORIZON_H`, the chart's copy unsuppressed | `sensor.py:246` `_published_spike_credible`; `camera.py:498` and `camera.py:530` | the short-lead tests from #177; `qld_short_lead_spike` after this spec |

## Part A: golden master

### Layout

```
tests/golden/
  __init__.py
  clock.py         # FrozenClock: one instant, patched into every wall-clock read
  scenarios.py     # Scenario builders: deterministic, synthetic inputs
  recorded.py      # loads tests/golden/recorded/*.json.gz (Part B output)
  harness.py       # builds the entry, runs platform setup, collects entities
  snapshot.py      # canonical serialisation and structured diff
  snapshots/<scenario>.json
tests/test_golden_master.py   # one parametrized test per scenario
```

### Interfaces

```python
@dataclass(frozen=True)
class Scenario:
    name: str
    region: str
    now: datetime                      # the frozen instant, NEM time
    options: Mapping[str, Any]         # config entry options (forecast mode, active tariff)
    pd7day: PD7DayResult               # prices, interconnectors, market summary, case
    stpasa: StpasaResult | None
    calibration: CalibrationSeed       # storage payloads, or observations to fit
    dispatch: Mapping[str, DispatchPrice] | None
    notices: Sequence[MarketNotice]
    usage_fee: float | None            # the number entity's restored state
    stale: StaleState | None           # coordinator last-failure fields, for stale scenarios

class CalibrationSeed(Protocol):
    def load_into(self, store: CalibrationStore) -> Awaitable[None]: ...

def build_entities(scenario: Scenario) -> list[Entity]: ...
def snapshot(entities: Sequence[Entity], scenario: Scenario) -> dict[str, Any]: ...
def diff(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]: ...
```

`build_entities` uses the real objects wherever the network is not involved: a real `PD7DayCoordinator` whose `data` is set from the scenario rather than fetched, a real `CalibrationStore` loaded through `support.MemoryStore`, a real `DispatchCoordinator` holding the scenario's prices, and the real `async_setup_entry` of each of the four platforms. Only Home Assistant itself is stubbed, through `tests/support.py`.

### What a snapshot records

For every entity the platforms create, keyed by unique id: `entity_id` suggestion and `name`, `native_value` (or `is_on`, or the number's value), every key of `extra_state_attributes`, `entity_registry_enabled_default`, `device_class`, `state_class`, `native_unit_of_measurement`, `entity_category`, and the device identifiers. For the camera, the entries `_build_forecast_data()` hands the renderer, plus the SHA-256 of the rendered PNG.

The snapshot therefore holds both views of `spike_credible`: the published one in each forecast sensor's `forecast` attribute, and the unsuppressed one in the camera entries. They are recorded separately and never derived from each other, so a refactor that makes one follow the other fails. An interval with no `spike_credible` key (raw below `SPIKE_THRESHOLD`) is recorded as absent, which is distinct from a recorded `null`.

### Canonical form

- JSON with sorted keys and two-space indent, one file per scenario.
- Floats written with `repr`, so equality is exact to the last bit. No rounding, no tolerance: a refactor that moves a value by one ulp fails, on purpose.
- Datetimes as ISO 8601 with offset; sets as sorted lists; tuples as lists.
- The PNG hash is valid only on the pinned `matplotlib==3.11.1`; the harness asserts that version before comparing it.
- Nothing is excluded. If a field turns out not to be deterministic under the frozen clock, that is a defect to fix in the harness (usually an unfrozen clock read), not a field to drop.

### Frozen clock

`FrozenClock` patches every wall-clock read in the package to the scenario's instant. Today those are in 23 modules; the largest are `coordinator.py` (12 reads), `tariff_sensor.py` (8), `sensor.py` (8), `market_notice_client.py` (6) and `calibration_store.py` (5). The clock module enumerates them from source with an AST scan at import, patches each, and fails the run if a read exists that it does not know how to patch, so a new unfrozen read cannot slip in silently. Some modules wrap the clock locally (for example `calibration_store._now_nem` at `calibration_store.py:62`), so the scan patches the wrapper as well as the call it wraps.

### Scenarios (synthetic tier)

Each is small and names what it exercises. Thirteen to start:

| Name | Exercises |
|---|---|
| `qld_fitted_evening_peak` | QLD1, stage 1 and stage 2 fitted, days 1-7, Energex tariffs, evening peak |
| `qld_days27_mode` | the day 2-7 sensors and trims |
| `qld_empty_store` | no calibration yet: passthrough everywhere |
| `qld_spike_credible` | raw above the spike threshold, gas high, network tight (#176) |
| `qld_spike_uncredible` | the same spike, network slack |
| `qld_short_lead_spike` | days 1-7 mode, one credible spike under `SPIKE_COVARIATE_MIN_HORIZON_H` and one beyond it: the sensor attribute reads `null` for the first and `true` for the second, and the chart entries read `true` for both (#177) |
| `qld_negative_midday` | negative prices, below-domain clip, band floor (#114) |
| `qld_stage2_out_of_domain` | stage 2 declined by the feature-domain gate (#147, #153) |
| `nsw_dispatch_live` | a dispatch price present: native values follow dispatch |
| `sa_stale_coordinator` | stale data: `is_stale`, `stale_reason`, `data_age_hours` |
| `vic_prcer_extension` | Powercor PRCER import and export from the extension (#170, #172) |
| `vic_spike_region_interconnectors` | a VIC1 spike scored on VIC1's own interconnectors, so the gate returns a boolean where it once returned only `null` (#176, #177) |
| `qld_lor2_notice` | a current LOR2 notice on the binary sensor and chart |

`qld_fitted_evening_peak` also pins the experimental scarcity premium sensor (#179), which every QLD1 entry creates. If its morning trigger is not met at that instant, `qld_short_lead_spike` or a fourteenth scenario must meet it, so the snapshot holds a non-zero premium as well as the passthrough.

Observations for the fitted scenarios are generated from a fixed seed, then fitted with the real engine, so the fit itself is part of what the snapshot pins.

### Update rule

`GOLDEN_UPDATE=1 pytest tests/test_golden_master.py` rewrites the snapshots. A pull request whose title starts `refactor:` must not change anything under `tests/golden/snapshots/`; CI enforces this (Part C). Any other PR that changes a snapshot must say in its description which values move and why.

## Part B: recorded inputs

### Why a service

The golden master should include real runs, and the real inputs exist only on the live install: the integration persists the last PD7DAY result, the STPASA result, the calibration observations, coefficients and forecast history, and the notices, all in Home Assistant's storage. This sandbox cannot reach NEMWEB, and the tools available here cannot read `.storage`, so the inputs have to leave through the integration itself.

### Interface

```yaml
# services.yaml
export_golden_inputs:
  fields:
    entry_id: {required: true}
```

```python
async def _handle_export_golden_inputs(call: ServiceCall) -> ServiceResponse: ...
# registered with supports_response=SupportsResponse.ONLY
```

The response is one JSON object for the entry: the payloads exactly as each store would write them (`forecast_store`, `stpasa_store`, the three calibration stores, the observation log segments, the notice store), plus the current dispatch prices, the usage fee number's state, the entry's options, `library_version()`, and the instant of export. `scripts/golden_record.py` turns a response into `tests/golden/recorded/<region>-<date>.json.gz`, and `recorded.py` turns that into a `Scenario` whose frozen instant is the export instant.

### Replay for the live gate

The plan's live gate replays exports taken at four instants over the 24 hours after a deploy through the previous and the new release. `scripts/golden_replay.py <ref-a> <ref-b> <export>...` builds a worktree for each git ref, runs the harness over each export in both, and prints the structured diff; an empty diff passes. Replay exports are kept outside the repository; only the one recorded scenario per region below is committed.

### What the live install covers

The live install has five entries. QLD1, NSW1, SA1 and VIC1 run in days 2-7 mode, so their forecasts start about 34 hours out; TAS1 runs in days 1-7 mode. Recorded scenarios and the live replay therefore exercise short-lead behaviour only through TAS1, which rarely spikes. Short-lead behaviour in the mainland regions is covered by the synthetic tier (`qld_short_lead_spike`), and each later spec states which synthetic scenario covers any mode the live install does not run.

### Invariants

- Read-only: the handler reads the in-memory state and the stores; it writes nothing and schedules nothing.
- Additive: no existing entity, attribute, service or stored format changes. `force_refit` behaves as before.
- Nothing personal is exported. Every payload is public market data or the integration's own fitted state; the only user-set value is the usage fee.
- Size: recorded fixtures together stay under 2 MB compressed. If a region's observation log would exceed its share, the recorder keeps the coefficient store and forecast history (enough to reproduce serving) and drops the observations, and the scenario is marked serving-only.

## Part C: gate tooling

| Tool | File | CI behaviour |
|---|---|---|
| Coverage comparison | `scripts/cov_compare.py` | new job on pull requests: run the suite with `--cov` on the base and on the head in parallel, fail if any line executed on the base is unexecuted on the head. Line numbers change when code moves, so the comparison is per function, keyed by qualified name, and reads an optional move map (`old.module:qualname -> new.module:qualname`) from a fenced block in the spec the PR names |
| Size ratchet | `scripts/size_check.py`, `scripts/size_baseline.json` | fail on a new function over 60 lines or class over 250 lines or 15 methods; the baseline lists today's offenders with their sizes (39 functions and 8 classes at `1179345`), and an entry may shrink or disappear but never grow or be added |
| Import contract | `.importlinter`, `import-linter` pinned in `requirements-test.txt` | `lint-imports` runs and reports; `continue-on-error` until spec 008 turns it into a failure |
| Golden untouched | `scripts/check_golden_untouched.py` | on a PR titled `refactor:`, fail if `tests/golden/snapshots/` differs from the base |
| Assertions untouched | `scripts/check_assertions_untouched.py` | on a PR titled `refactor:`, fail if any `assert` statement or `pytest.raises` block in `tests/` differs from the base, compared by AST per test function; patch targets and imports may change, and each changed one must appear in the spec's retarget list |
| mypy ratchet | existing `scripts/mypy_ratchet.py`, extended | the total still may not rise; the script also writes per-module counts, so the plan's "strictly lower where the spec's modules carry errors" and "zero in new modules" are checked against the base run rather than by eye |

### Initial layer map

Measured on `main` after #169. The 17 modules that import `homeassistant` start in the adapter layer: `__init__`, `actual_price_service`, `binary_sensor`, `calibration_store`, `camera`, `config_flow`, `coordinator`, `diagnostics`, `fetch_scheduler`, `forecast_store`, `notice_store`, `number`, `observation_log`, `scarcity_sensor`, `sensor`, `stpasa_store`, `tariff_sensor`.

The seven network clients also start in the adapter layer, as the plan places them, although they do not import `homeassistant`: `dispatch_client`, `market_notice_client`, `nemweb_gate`, `nemweb_retry`, `pd7day_client`, `stpasa_client`, `tradingis_client`.

The remaining 16 start in the lower two layers. Domain: `const`, `nem_time`, `calibration_engine`, `tariff_catalogue`, `tariff_extensions`, `scarcity_premium`, `tod_stats`. Services: `calibration_inputs`, `executor`, `pd7day_shared`, `shared_dispatch`, `startup_trace`, `stpasa_refresh`, `bias_chart`, `forecast_chart`, `iso_chart`. The contract restricts imports within the package only; third-party libraries such as `numpy`, `astral`, `matplotlib` and `aemo_to_tariff` are not restricted.

The report run is expected to show violations, for example `calibration_inputs` (services) importing `coordinator` (adapters); each one is a known item for a later spec, recorded in the first report and not fixed here.

## Migration

0. Confirm the housekeeping PR is merged, so the snapshots and baselines below are taken without the dead spike constants and with `mypy_baseline.txt` at 59.
1. Add `tests/golden/` with the clock, the harness, the snapshot code and the thirteen scenarios; generate snapshots on `main`; commit them.
2. Run the golden master twice in a row and in reverse scenario order; both must pass without regeneration, which proves determinism.
3. Add the Part C scripts and CI jobs, with import-linter in report mode.
4. Add the Part B service and recorder, with tests that it is read-only and additive.
5. Deploy, call the service for each of the five live entries, record, add one recorded scenario per region, and commit their snapshots.
6. Run `golden_replay.py` over the five exports with the release before this spec and the release that carries it; the diff must be empty (the new service adds no entity, so it does not appear in a snapshot), which proves the replay tool before any refactor relies on it.

## Non-goals

- No refactor of any source module. Where the harness finds code that is awkward to drive, it drives it anyway and the awkwardness goes into the relevant later spec.
- No fix for anything the harness exposes. Nondeterminism in the product, or a value that looks wrong, is filed as an issue and linked here.
- No change to #171 (`daily_supply_charge_$` in cents): the snapshot pins today's value, and the fix will be a deliberate snapshot update in its own PR.
- No parser coverage. The golden master starts from parsed results; the parsers get their own net in spec 010.
- No change to the split between the sensor and chart views of `spike_credible`. The snapshot pins both as they are.

## Acceptance

- [ ] Thirteen synthetic scenarios, each passing twice in a row and in reverse order with no regeneration.
- [ ] `qld_short_lead_spike` shows the sensor attribute `null` and the chart entry `true` for the short-lead spike, and the snapshot holds a non-zero scarcity premium in at least one scenario.
- [ ] A deliberate one-ulp change to one published float in a scratch branch fails the golden master with a diff naming the entity and attribute.
- [ ] Every wall-clock read in the package is frozen; the AST scan finds none unpatched.
- [ ] Full suite passes; no existing assertion edited; new tests: `tests/test_golden_master.py`, the Part B service tests, and the Part C script tests.
- [ ] No source line executed before is unexecuted after.
- [ ] mypy count does not rise; zero errors in the new service code.
- [ ] `size_baseline.json` matches today's offenders (39 functions, 8 classes); `check_golden_untouched.py`, `check_assertions_untouched.py` and `cov_compare.py` each fail on a deliberate violation in a scratch branch, and `cov_compare.py` passes a scratch branch that only moves one function under a move map.
- [ ] `mypy_ratchet.py` writes per-module counts, and its total equals the value in `mypy_baseline.txt`.
- [ ] Import-linter's first report committed as `docs/architecture/import-report-000.txt`.
- [ ] `export_golden_inputs` is read-only (a test asserts no store is saved and no task scheduled) and returns the documented keys.
- [ ] Five recorded scenarios, one per live region, each passing, together under 2 MB.
- [ ] `golden_replay.py` gives an empty diff between the release before this spec and the release that carries it.

## Rollback

Parts A and C are test and CI files: revert the commit. Part B adds a service with no stored state; reverting removes it, and any recorded fixtures stay valid as test data.
