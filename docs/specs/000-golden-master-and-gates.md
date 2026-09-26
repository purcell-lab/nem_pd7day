# Spec 000: Golden master and refactor gates

Status: approved 26 September 2026; Part A implemented (this PR)
Plan: docs/architecture/tech-debt-plan.md, step 000

## Responsibility

Build the safety net every later spec is accepted against, and nothing else. Three parts:

- **A. Golden master.** A test that runs the real platform setup code over fixed inputs with a frozen clock, and compares every entity's observable output to a committed snapshot, exactly.
- **B. Recorded inputs.** An opt-in, read-only service that exports one config entry's persisted inputs from the live install, so the golden master can include real runs as well as synthetic ones.
- **C. Gate tooling.** The coverage comparison, the size ratchet and the import contract, wired into CI.

Part A can land alone; B and C follow in the same PR or the next one. No spec from 001 onwards starts until all three are merged.

## Current behaviour this must preserve

Spec 000 changes no published value. Parts A and C are test and CI files only. Part B adds one service and touches nothing else.

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Every entity's state, attributes, name, unique id, enabled default | `sensor.py`, `tariff_sensor.py`, `scarcity_sensor.py`, `binary_sensor.py`, `number.py`, `camera.py` | the full suite today; the golden master after this spec |
| The four platforms and nothing else | `__init__.py:57` `PLATFORMS` | `tests/test_lifecycle.py` |
| `force_refit` is the only registered service | `__init__.py:549` | `tests/test_lifecycle.py`; Part B adds a second, asserted by a new test |

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
  snapshots/<scenario>.json.gz
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

### Canonical form

- JSON with sorted keys and two-space indent, one file per scenario, gzipped with `mtime=0` so the file bytes are a pure function of the content (the tariff sensors publish whole forecasts; plain JSON was 7.8 MB, gzipped 443 KB). The test prints its own structured diff, so line-diffable files would add nothing.
- Floats written with `repr`, so equality is exact to the last bit. No rounding, no tolerance: a refactor that moves a value by one ulp fails, on purpose.
- Datetimes as ISO 8601 with offset; sets as sorted lists; tuples as lists.
- The PNG hash is valid only on the pinned `matplotlib==3.11.1`; the harness asserts that version before comparing it.
- Snapshots are recorded on CPython 3.13, the version CI and Home Assistant run. CPython 3.12 changed `sum()` of floats to compensated summation, so the product's own means differ in the last bit on 3.11; the comparison skips below 3.12 with that reason rather than carrying a tolerance.
- Scenario inputs are built from `+ - * /` and comparisons only. libm's `sin`, `exp`, `tanh` and `log` may differ by an ulp between C libraries, and CI's first run showed such an ulp flipping a value published at six decimals.
- Nothing is excluded. If a field turns out not to be deterministic under the frozen clock, that is a defect to fix in the harness (usually an unfrozen clock read), not a field to drop.

### Frozen clock

`FrozenClock` patches every wall-clock read in the package to the scenario's instant. Today those are in 23 modules; the largest are `coordinator.py` (12 reads), `tariff_sensor.py` (8), `sensor.py` (8), `market_notice_client.py` (6) and `calibration_store.py` (5). The clock module enumerates them from source with an AST scan at import, patches each, and fails the run if a read exists that it does not know how to patch, so a new unfrozen read cannot slip in silently.

### Scenarios (synthetic tier)

Each is small and names what it exercises. Twelve to start:

| Name | Exercises |
|---|---|
| `qld_fitted_evening_peak` | QLD1, stage 1 and stage 2 fitted, days 1-7, Energex tariffs, evening peak |
| `qld_days27_mode` | the day 2-7 sensors and trims |
| `qld_empty_store` | no calibration yet: passthrough everywhere |
| `qld_spike_credible` | raw above the spike threshold, gas high, network tight (#176) |
| `qld_spike_uncredible` | the same spike, network slack |
| `qld_short_lead_spike` | a spike under the covariate minimum horizon |
| `qld_negative_midday` | negative prices, below-domain clip, band floor (#114) |
| `qld_stage2_out_of_domain` | stage 2 declined by the feature-domain gate (#147, #153) |
| `nsw_dispatch_live` | a dispatch price present: native values follow dispatch |
| `sa_stale_coordinator` | stale data: `is_stale`, `stale_reason`, `data_age_hours` |
| `vic_prcer_extension` | Powercor PRCER import and export from the extension (#170) |
| `qld_lor2_notice` | a current LOR2 notice on the binary sensor and chart |

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

### Invariants

- Read-only: the handler reads the in-memory state and the stores; it writes nothing and schedules nothing.
- Additive: no existing entity, attribute, service or stored format changes. `force_refit` behaves as before.
- Nothing personal is exported. Every payload is public market data or the integration's own fitted state; the only user-set value is the usage fee.
- Size: recorded fixtures together stay under 2 MB compressed. If a region's observation log would exceed its share, the recorder keeps the coefficient store and forecast history (enough to reproduce serving) and drops the observations, and the scenario is marked serving-only.

## Part C: gate tooling

| Tool | File | CI behaviour |
|---|---|---|
| Coverage comparison | `scripts/cov_compare.py` | new job on pull requests: run the suite with `--cov` on the base and on the head in parallel, fail if any line executed on the base is unexecuted on the head |
| Size ratchet | `scripts/size_check.py`, `scripts/size_baseline.json` | fail on a new function over 60 lines or class over 250 lines or 15 methods; the baseline lists today's offenders with their sizes, and an entry may shrink or disappear but never grow or be added |
| Import contract | `.importlinter`, `import-linter` pinned in `requirements-test.txt` | `lint-imports` runs and reports; `continue-on-error` until spec 008 turns it into a failure |
| Golden untouched | `scripts/check_golden_untouched.py` | on a PR titled `refactor:`, fail if `tests/golden/snapshots/` differs from the base |
| mypy ratchet | existing | unchanged |

### Initial layer map

Measured on `main` after #169. The 17 modules that import `homeassistant` start in the adapter layer: `__init__`, `actual_price_service`, `binary_sensor`, `calibration_store`, `camera`, `config_flow`, `coordinator`, `diagnostics`, `fetch_scheduler`, `forecast_store`, `notice_store`, `number`, `observation_log`, `scarcity_sensor`, `sensor`, `stpasa_store`, `tariff_sensor`.

The other 23 start in the lower two layers: `const`, `nem_time`, `calibration_engine`, `tariff_catalogue`, `tariff_extensions`, `scarcity_premium` and `tod_stats` in the domain layer; the rest in services. The report run is expected to show violations, for example `calibration_inputs` (services) importing `coordinator` (adapters); each one is a known item for a later spec, recorded in the first report and not fixed here.

## Migration

1. Add `tests/golden/` with the clock, the harness, the snapshot code and the twelve scenarios; generate snapshots on `main`; commit them.
2. Run the golden master twice in a row and in reverse scenario order; both must pass without regeneration, which proves determinism.
3. Add the Part C scripts and CI jobs, with import-linter in report mode.
4. Add the Part B service and recorder, with tests that it is read-only and additive.
5. Deploy, call the service for each of the five live entries, record, add one recorded scenario per region, and commit their snapshots.

## Non-goals

- No refactor of any source module. Where the harness finds code that is awkward to drive, it drives it anyway and the awkwardness goes into the relevant later spec.
- No fix for anything the harness exposes. Nondeterminism in the product, or a value that looks wrong, is filed as an issue and linked here.
- No change to #171 (`daily_supply_charge_$` in cents): the snapshot pins today's value, and the fix will be a deliberate snapshot update in its own PR.
- Found while building Part A and pinned as they are, each to be fixed in its own PR with a deliberate snapshot update: #181 (the tariff sensors never find the usage-fee number, confirmed live) and #182 (a cancellation naming a notice id also cancels every same-level notice that day).

## Acceptance

- [ ] Twelve synthetic scenarios, each passing twice in a row and in reverse order with no regeneration.
- [ ] A deliberate one-ulp change to one published float in a scratch branch fails the golden master with a diff naming the entity and attribute.
- [ ] Every wall-clock read in the package is frozen; the AST scan finds none unpatched.
- [ ] Full suite passes; no existing assertion edited; new tests: `tests/test_golden_master.py`, the Part B service tests, and the Part C script tests.
- [ ] No source line executed before is unexecuted after.
- [ ] mypy count does not rise; zero errors in the new service code.
- [ ] `size_baseline.json` matches today's offenders; `check_golden_untouched.py` and `cov_compare.py` each fail on a deliberate violation in a scratch branch.
- [ ] Import-linter's first report committed as `docs/architecture/import-report-000.txt`.
- [ ] `export_golden_inputs` is read-only (a test asserts no store is saved and no task scheduled) and returns the documented keys.
- [ ] Five recorded scenarios, one per live region, each passing, together under 2 MB.

## Rollback

Parts A and C are test and CI files: revert the commit. Part B adds a service with no stored state; reverting removes it, and any recorded fixtures stay valid as test data.
