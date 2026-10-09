# Spec 000: Golden master and refactor gates

Status: approved 26 September 2026; Part A implemented (#183); Part C implemented (#191) apart from the assertions and per-module mypy checks; Part B not started; Part D proposed 27 September 2026, inputs harvested
Plan: docs/architecture/tech-debt-plan.md, step 000
Measured on: `main` at `1179345` (v3.17.0 plus #169); Part A as merged at `d9f5392`
Depends on: Parts B and C follow the housekeeping PR in plan section 4 (dead spike constants removed, stale `camera._build_forecast_data` docstring rewritten, `mypy_baseline.txt` at 59), so their baselines are taken after it. The constants are unused, so the Part A snapshots do not move.

## Responsibility

Build the safety net every later spec is accepted against, and nothing else. Four parts:

- **A. Golden master.** A test that runs the real platform setup code over fixed inputs with a frozen clock, and compares every entity's observable output to a committed snapshot, exactly.
- **B. Recorded inputs.** An opt-in, read-only service that exports one config entry's persisted inputs from the live install, so the golden master can include real runs as well as synthetic ones.
- **C. Gate tooling.** The coverage comparison, the size ratchet, the import contract, the assertion guard and the per-module mypy counts, wired into CI.
- **D. Real market inputs.** Real PD7DAY, STPASA, price and notice files from NEMWEB, trimmed and committed, run through the same harness, so the golden master holds real market shapes (mainland spikes at short lead among them) as well as synthetic ones.

Part A has landed (#183). No spec from 001 onwards starts until B and C are merged. D must be merged before spec 004, the first to move the spike annotation, and before spec 010, which moves the parsers.

The synthetic and recorded tiers start from parsed inputs, so they do not cover the NEMWEB parsers. Part D starts from the files and runs them through the real parsers, so a parser change that alters an entity fails there; pinning each parser's own output field by field still belongs to spec 010.

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

The snapshot therefore holds both views of `spike_credible`: the published one in each forecast sensor's `forecast` attribute, and the unsuppressed one in the camera entries. They are recorded separately and never derived from each other, so a refactor that makes one follow the other fails. An interval with no `spike_credible` key (raw below `SPIKE_THRESHOLD`) is recorded as absent, which is distinct from a recorded `null`.

### Canonical form

- JSON with sorted keys and two-space indent, one file per scenario, gzipped with `mtime=0` so the file bytes are a pure function of the content (the tariff sensors publish whole forecasts; plain JSON was 7.8 MB, gzipped 443 KB). The test prints its own structured diff, so line-diffable files would add nothing.
- Floats written with `repr`, so equality is exact to the last bit. No rounding, no tolerance: a refactor that moves a value by one ulp fails, on purpose.
- Datetimes as ISO 8601 with offset; sets as sorted lists; tuples as lists.
- The PNG hash is valid only on the pinned `matplotlib==3.11.1`; the harness asserts that version before comparing it.
- Snapshots are recorded on CPython 3.13, the version CI and Home Assistant run. CPython 3.12 changed `sum()` of floats to compensated summation, so the product's own means differ in the last bit on 3.11; the comparison skips below 3.12 with that reason rather than carrying a tolerance.
- `tests/conftest.py` pins the floating-point environment before numpy loads: one OpenBLAS thread, the Haswell (AVX2) kernel, and numpy capped at X86_V3. Without this the ill-conditioned stage-2 `lstsq` coefficients differ in their last bits between an AVX-512 machine and a GitHub runner.
- Scenario inputs are built from `+ - * /` and comparisons only. libm's `sin`, `exp`, `tanh` and `log` may differ by an ulp between C libraries, and CI's first run showed such an ulp flipping a value published at six decimals.
- Nothing is excluded. If a field turns out not to be deterministic under the frozen clock, that is a defect to fix in the harness (usually an unfrozen clock read), not a field to drop.

### Frozen clock

`FrozenClock` patches every wall-clock read in the package to the scenario's instant. As merged, the scan finds 79 reads: 66 in the integration across 23 modules and 13 in `aemo_to_tariff`. The largest in the integration are `coordinator.py` (12 reads), `tariff_sensor.py` (8), `sensor.py` (8), `market_notice_client.py` (6) and `calibration_store.py` (5). The clock module enumerates them from source with an AST scan at import, patches each, and fails the run if a read exists that it does not know how to patch, so a new unfrozen read cannot slip in silently. Some modules wrap the clock locally (for example `calibration_store._now_nem` at `calibration_store.py:62`), so the scan patches the wrapper as well as the call it wraps.

### Scenarios (synthetic tier)

Each is small and names what it exercises. Twelve, as merged in #183:

| Name | Exercises |
|---|---|
| `qld_fitted_evening_peak` | QLD1, stage 1 and stage 2 fitted, days 1-7, Energex tariffs, evening peak |
| `qld_days27_mode` | the day 2-7 sensors and trims |
| `qld_days27_amber_joined` | the day 2-7 series starting where an Amber Electric forecast found in the entity registry ends (#235) |
| `qld_empty_store` | no calibration yet: passthrough everywhere |
| `qld_spike_credible` | raw above the spike threshold, gas high, network tight (#176) |
| `qld_spike_uncredible` | the same spike, network slack |
| `qld_short_lead_spike` | days 1-7 mode, two spikes under `SPIKE_COVARIATE_MIN_HORIZON_H` with the covariates supporting them: the sensor attribute reads `null`, the chart entries read `true` (#177) |
| `qld_negative_midday` | negative prices, below-domain clip, band floor (#114) |
| `qld_stage2_out_of_domain` | stage 2 declined by the feature-domain gate (#147, #153) |
| `nsw_dispatch_live` | a dispatch price present: native values follow dispatch |
| `sa_stale_coordinator` | stale data: `is_stale`, `stale_reason`, `data_age_hours` |
| `vic_prcer_extension` | Powercor PRCER import and export from the extension (#170, #172) |
| `qld_lor2_notice` | a current LOR2 notice on the binary sensor and chart |

Every QLD1 scenario pins the experimental scarcity premium sensor (#179). `qld_negative_midday` meets its morning trigger and asserts status `active` with a state above zero, so the snapshot holds a non-zero premium as well as the passthrough. A VIC1 spike scored on VIC1's own interconnectors (#176, #177) has no synthetic scenario; Part D covers it with real runs.

Observations for the fitted scenarios are generated from a fixed seed, then fitted with the real engine, so the fit itself is part of what the snapshot pins.

### Update rule

`GOLDEN_UPDATE=1 pytest tests/test_golden_master.py` rewrites the snapshots. A pull request whose title starts `refactor:` must not change anything under `tests/golden/snapshots/`; CI enforces this (Part C). Any other PR that changes a snapshot must say in its description which values move and why.

## Part B: recorded inputs

### Why a service

The golden master should include real runs, and the real inputs exist only on the live install: the integration persists the last PD7DAY result, the STPASA result, the calibration observations, coefficients and forecast history, and the notices, all in Home Assistant's storage. The market files are public and Part D takes them from NEMWEB, but the calibration state, the notice store, the options and the usage fee exist only on the live install. The tools available here cannot read `.storage`, so that state has to leave through the integration itself. The export's market payloads can be checked against the same NEMWEB files.

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

The live install has five entries. QLD1, NSW1, SA1 and VIC1 run in days 2-7 mode, so their forecasts start about 34 hours out; TAS1 runs in days 1-7 mode. Recorded scenarios and the live replay therefore exercise short-lead behaviour only through TAS1, which rarely spikes. Short-lead behaviour in the mainland regions is covered by the synthetic tier (`qld_short_lead_spike`) and by Part D, which runs real mainland spikes in days 1-7 mode. Each later spec states which synthetic or Part D scenario covers any mode the live install does not run.

### Invariants

- Read-only: the handler reads the in-memory state and the stores; it writes nothing and schedules nothing.
- Additive: no existing entity, attribute, service or stored format changes. `force_refit` behaves as before.
- Nothing personal is exported. Every payload is public market data or the integration's own fitted state; the only user-set value is the usage fee.
- Size: recorded fixtures together stay under 2 MB compressed. If a region's observation log would exceed its share, the recorder keeps the coefficient store and forecast history (enough to reproduce serving) and drops the observations, and the scenario is marked serving-only.

## Part D: real market inputs

### Why

The synthetic tier pins shapes that were built to exercise a path. Real NEMWEB runs carry shapes nobody designed: price cap plateaus, spikes that never arrive, interconnector patterns that differ by region. The live install cannot supply them for most modes, because four of its five entries run days 2-7. NEMWEB can: its files are public, and they can be run through the integration in any mode a scenario names.

### What NEMWEB keeps

Checked on 27 September 2026 (UTC+10):

| Feed | Current directory | Archive |
|---|---|---|
| PD7DAY | about 60 days, three runs a day (180 files from 29 July) | none: `Reports/Archive/PD7Day/` returns 404 |
| STPASA | about 60 days, hourly (1,440 files) | monthly zips from August 2025 |
| TradingIS | recent days | weekly zips |
| DispatchIS | recent days | daily zips for a year |
| Market notices | about 60 days | none |

A PD7DAY run older than about 60 days is gone for good. Part D therefore starts with a harvest, taken before any scenario is written.

### Harvest

`scripts/nemweb_harvest.py` saves every file still listed for PD7DAY, STPASA and market notices, and the TradingIS weekly archives covering the same window. It keeps only what the integration reads: for PD7DAY, `CASESOLUTION`, `MARKET_SUMMARY`, `INTERCONNECTORSOLUTION` and `PRICESOLUTION` (`CONSTRAINTSOLUTION` is dropped: it is parsed but unused, and most of each 43 MB file); for STPASA, `REGIONSOLUTION`; for TradingIS, the `TRADING,PRICE` rows. A trimmed PD7DAY run is about 90 kB gzipped, against 4.2 to 4.8 MB for the original zip. Each trimmed file keeps the NEMWEB `C` and `I` rows unchanged, so the real parsers read it as they read the original.

A `manifest.jsonl` beside each feed records, per file, the source URL, the original zip's size and SHA-256, and the row counts kept. The full harvest lives outside the repository; the location is settled in the Part D PR.

The first harvest (26 to 27 September 2026) covers 29 July to 27 September. In 178 of the 180 PD7DAY runs, at least one mainland region has intervals forecast at $20,000/MWh or more, most at the $23,200/MWh market price cap, and TradingIS shows almost none arrived: from 20 July to 19 September the only 5-minute prices above $1,000/MWh were two SA1 intervals on 31 July ($4,981 ending 02:35 and $3,844 ending 02:55). That is the population the spike annotation exists for.

### Scenarios

Scenarios live in `tests/golden/nemweb/<scenario>/`, holding the files they use and a copy of their manifest rows: the PD7DAY run, the STPASA run published at or before it, the region's TradingIS prices for the eight days from the run day, and the market notices created in the 48 hours up to the run. `nemweb_harvest.py extract` builds a folder from the harvest. `tests/golden/nemweb.py` turns one into a `Scenario`: the PD7DAY run and the STPASA run before it go through the real parsers (`pd7day_client`, `stpasa_client`) via the harness's existing client stubs, the frozen instant is the PD7DAY run time, and the options are the scenario's own. Calibration comes from the synthetic observation seed unless the scenario says otherwise, so only the market inputs are real.

Four to start, one per mainland live region:

| Name | Source run (NEM time) | Exercises |
|---|---|---|
| `qld_nemweb_short_lead_spike` | PD7DAY 2026-08-05 07:30 | days 1-7 mode, QLD1: two intervals at or above `SPIKE_THRESHOLD` ($3,000/MWh) that start under 24 h out (23.0 h at $19,433, 23.5 h at $8,999) and 21 beyond; the sensor attribute reads `null` for the first group and a boolean for the second, the chart reads a boolean for both (#177) |
| `vic_nemweb_spike_interconnectors` | PD7DAY 2026-07-30 07:30 | days 1-7 mode, VIC1 scored on VIC1's own interconnectors (#176, #177): four threshold intervals under 24 h (from 11.5 h, to $23,200) and 45 beyond |
| `sa_nemweb_cap_plateau` | PD7DAY 2026-07-30 07:30 | days 2-7 mode as live, SA1: 81 threshold intervals beyond 24 h, with plateaus at the price cap on the morning and evening of 31 July; the real price in those periods stayed under $1,000/MWh, and SA1's only real spike ($4,981 for the interval ending 02:35 on 31 July) came when this run forecast under $300 |
| `nsw_nemweb_days27_endeavour` | PD7DAY 2026-08-05 07:30 | days 2-7 mode as live, NSW1 with Endeavour N71: 46 threshold intervals beyond 24 h, most at the price cap |

Counts are for the no-intervention case, by interval start (`time`) against the run time, as the integration measures horizon. They were checked by passing the committed files through today's `pd7day_client._parse_all_tables` and `calibration_inputs.horizon_hours`; the STPASA files parse to all five regions, 288 intervals each. None of the notices in these windows is an LOR or MSL notice, so the notice path is exercised only as far as parsing and discarding.

Each carries a non-vacuity assertion like the synthetic ones: the named intervals are present, and the named annotation or price path is reached. A scenario that stops reaching its path fails.

### Invariants

- No test touches the network. The harness already stubs `aiohttp`; the Part D loader also fails if a scenario names a file that is not committed.
- Committed files are trimmed NEMWEB output and nothing else; each carries its manifest row, so anyone can fetch the original while NEMWEB still lists it and confirm the SHA-256.
- Size: Part D fixtures together stay under 2 MB compressed, separate from Part B's budget.
- A parser change that alters any entity in a Part D scenario is a behaviour change and needs a deliberate snapshot update, like any other.

## Part C: gate tooling

| Tool | File | CI behaviour |
|---|---|---|
| Coverage comparison | `scripts/cov_compare.py` | new job on pull requests: run the suite with `--cov` on the base and on the head, fail if any line executed on the base is unexecuted on the head. Line numbers change when code moves, so each base line is mapped to its head line through a diff of the two versions of its file. Executed lines the change deletes or edits are listed but do not fail, so code moved to another module is checked by the coverage of the module it moves to |
| Size ratchet | `scripts/size_check.py`, `scripts/size_baseline.json` | fail on a new function over 60 lines or class over 250 lines or 15 methods; the baseline lists today's offenders with their sizes (39 functions and 8 classes at `1179345`), and an entry may shrink or disappear but never grow or be added |
| Import contract | `.importlinter`, `import-linter` pinned in `requirements-lint.txt` (it is a lint tool and runs in the lint job) | `lint-imports` runs and reports; `continue-on-error` until spec 008 turns it into a failure |
| Golden untouched | `scripts/check_golden_untouched.py` | on a PR titled `refactor:`, fail if `tests/golden/snapshots/` differs from the base |
| Assertions untouched | `scripts/check_assertions_untouched.py` | on a PR titled `refactor:`, fail if any `assert` statement or `pytest.raises` block in `tests/` differs from the base, compared by AST per test function; patch targets and imports may change, and each changed one must appear in the spec's retarget list |
| mypy ratchet | existing `scripts/mypy_ratchet.py`, extended | the total still may not rise; the script also writes per-module counts, so the plan's "strictly lower where the spec's modules carry errors" and "zero in new modules" are checked against the base run rather than by eye |

### As implemented

- `scripts/cov_compare.py` maps every executed base line to its head line through a diff of the two versions of the file, so code a change moves is not reported as lost, and executed lines the change edits or deletes are listed but do not fail. The CI job runs the suite with `--cov` on the head, then on the base in a worktree with the same tooling, then compares.
- `scripts/size_check.py` measures functions (methods and nested functions included) and classes by AST. `scripts/size_baseline.json` holds today's 39 functions and 8 classes over the limits; `--update` tightens it and refuses to loosen it, and on a pull request `--base-baseline` fails a baseline that adds or grows an entry against the base branch's copy.
- `.importlinter` is rooted at `custom_components`, because grimp accepts only a top-level package; the contracts name `custom_components.nem_pd7day.*` modules. A second, forbidden-import contract states the plan's rule directly: nothing below the adapter layer imports `homeassistant`.
- `scripts/check_golden_untouched.py` reads the pull request title from the environment, never inline, and fails a `refactor:` pull request that changes anything under `tests/golden/snapshots/`.
- `tests/test_gate_scripts.py` shows each gate failing on a deliberate violation and passing on clean input.
- Not built yet: `scripts/check_assertions_untouched.py` and the per-module counts in `mypy_ratchet.py`, both added to this table by #186 after #191 was written. Until they land, the verifier of each refactor PR checks both by hand: the assertion diff against the spec's retarget list, and the mypy count per touched module against a base run.

### Initial layer map

Measured on `main` after #169. The 17 modules that import `homeassistant` start in the adapter layer: `__init__`, `actual_price_service`, `binary_sensor`, `calibration_store`, `camera`, `config_flow`, `coordinator`, `diagnostics`, `fetch_scheduler`, `forecast_store`, `notice_store`, `number`, `observation_log`, `scarcity_sensor`, `sensor`, `stpasa_store`, `tariff_sensor`.

The seven network clients also start in the adapter layer, as the plan places them, although they do not import `homeassistant`: `dispatch_client`, `market_notice_client`, `nemweb_gate`, `nemweb_retry`, `pd7day_client`, `stpasa_client`, `tradingis_client`.

The remaining 16 start in the lower two layers. Domain: `const`, `nem_time`, `calibration_engine`, `tariff_catalogue`, `tariff_extensions`, `scarcity_premium`, `tod_stats`. Services: `calibration_inputs`, `executor`, `pd7day_shared`, `shared_dispatch`, `startup_trace`, `stpasa_refresh`, `bias_chart`, `forecast_chart`, `iso_chart`. The contract restricts imports within the package only; third-party libraries such as `numpy`, `astral`, `matplotlib` and `aemo_to_tariff` are not restricted.

The report run is expected to show violations, for example `calibration_inputs` (services) importing `coordinator` (adapters); each one is a known item for a later spec, recorded in the first report and not fixed here.

The first report (`docs/architecture/import-report-000.txt`) finds three layer violations: `calibration_inputs` and `shared_dispatch` import `coordinator`, and `calibration_engine` imports `stpasa_client`; the first two also reach `homeassistant` through `coordinator`.

## Migration

0. Confirm the housekeeping PR is merged, so the baselines below are taken without the dead spike constants and with `mypy_baseline.txt` at 59, and confirm the Part A snapshots did not move.
1. Done (#183): `tests/golden/` with the clock, the harness, the snapshot code and the twelve scenarios, snapshots generated on `main` and committed.
2. Run the golden master twice in a row and in reverse scenario order; both must pass without regeneration, which proves determinism.
3. Done (#191), apart from the assertions check and per-module mypy counts: the Part C scripts and CI jobs, with import-linter in report mode.
4. Add the Part B service and recorder, with tests that it is read-only and additive.
5. Deploy, call the service for each of the five live entries, record, add one recorded scenario per region, and commit their snapshots.
6. Run `golden_replay.py` over the five exports with the release before this spec and the release that carries it; the diff must be empty (the new service adds no entity, so it does not appear in a snapshot), which proves the replay tool before any refactor relies on it.
7. Done in the PR that approves this revision: harvest NEMWEB (26 to 27 September 2026, runs from 29 July), and commit `scripts/nemweb_harvest.py` and the input folders for the four Part D scenarios, about 1.1 MB, so the runs survive NEMWEB's 60-day window.
8. Add `tests/golden/nemweb.py` and the four scenarios with their non-vacuity assertions, and commit their snapshots.

## Non-goals

- No refactor of any source module. Where the harness finds code that is awkward to drive, it drives it anyway and the awkwardness goes into the relevant later spec.
- No fix for anything the harness exposes. Nondeterminism in the product, or a value that looks wrong, is filed as an issue and linked here.
- No change to #171 (`daily_supply_charge_$` in cents): the snapshot pins today's value, and the fix will be a deliberate snapshot update in its own PR.
- No per-field parser pinning. Part D runs real files through the parsers and pins what reaches the entities; the parsers' own outputs are pinned field by field in spec 010, which can use the Part D harvest.
- Found while building Part A and pinned as they are, each to be fixed in its own PR with a deliberate snapshot update: #181 (the tariff sensors never find the usage-fee number, confirmed live) and #182 (a cancellation naming a notice id also cancels every same-level notice that day).
- No change to the split between the sensor and chart views of `spike_credible`. The snapshot pins both as they are.

## Acceptance

- [x] Twelve synthetic scenarios passing on CPython 3.13 in CI (#183).
- [ ] Each passes twice in a row and in reverse order with no regeneration.
- [x] `qld_short_lead_spike` shows the sensor attribute `null` and the chart entry `true` for the short-lead spike, and the snapshot holds a non-zero scarcity premium in at least one scenario.
- [x] A deliberate one-ulp change to one published float in a scratch branch fails the golden master with a diff naming the entity and attribute.
- [x] Every wall-clock read in the package is frozen; the AST scan finds none unpatched (79 reads, #183).
- [ ] Full suite passes; no existing assertion edited; new tests: `tests/test_golden_master.py`, the Part B service tests, and the Part C script tests.
- [ ] No source line executed before is unexecuted after.
- [ ] mypy count does not rise; zero errors in the new service code.
- [ ] `size_baseline.json` matches today's offenders (39 functions, 8 classes); `check_golden_untouched.py`, `check_assertions_untouched.py` and `cov_compare.py` each fail on a deliberate violation in a scratch branch, and `cov_compare.py` passes a scratch branch that only moves one function under a move map.
- [ ] `mypy_ratchet.py` writes per-module counts, and its total equals the value in `mypy_baseline.txt`.
- [x] Import-linter's first report committed as `docs/architecture/import-report-000.txt`.
- [ ] `export_golden_inputs` is read-only (a test asserts no store is saved and no task scheduled) and returns the documented keys.
- [ ] Five recorded scenarios, one per live region, each passing, together under 2 MB.
- [ ] `golden_replay.py` gives an empty diff between the release before this spec and the release that carries it.
- [ ] Four Part D scenarios, each passing with its non-vacuity assertion, together under 2 MB, each file traceable through its manifest row to a NEMWEB URL and SHA-256.
- [ ] A Part D run with the network blocked passes.

## Rollback

Parts A and C are test and CI files: revert the commit. Part B adds a service with no stored state; reverting removes it, and any recorded fixtures stay valid as test data. Part D is test files and a harvest script: revert the commit; the harvest outside the repository is unaffected.
