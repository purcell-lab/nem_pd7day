# Technical debt plan: decomposing the god-class modules

Status: proposed, 26 September 2026, against `main` at v3.15.1.

This plan breaks the integration's largest modules into small units with one reason to change each, without changing a single published value or entity id. Every step is written as a specification first, implemented by an agent against that specification, and accepted only when a fixed set of automated gates pass.

## 1. Where the debt is

Measured on `main`. "Methods" counts methods defined on the class; "attrs" counts instance attributes assigned.

| Unit | Size | What it mixes |
|---|---|---|
| `calibration_store.CalibrationStore` | 702 lines, 24 methods, 12 attrs | persistence of three stores, forecast ingest, actual recording, refit orchestration, serving (`apply_to_price`), diagnostics summary |
| `tariff_sensor.NemPd7dayTariffSensor` | 601 lines, 25 methods | HA entity lifecycle, interval-boundary scheduling, calibrated-spot memo, TOU window lookup, retail price and GST, library/extension routing, attribute building |
| `tariff_sensor.NemPd7dayExportTariffSensor` | 322 lines, 17 methods | the same, with about 120 lines copied from the import sensor and three methods borrowed from it by assignment |
| `calibration_engine` | 2,185 lines | `CalibrationEngine.fit` 157 lines, `fit_ols_stage2` 224, `CalibrationResult.apply` 169 lines holding every serving gate from #67 to #153 in one body |
| `sensor.py` | 1,758 lines | `CalibratedWriteMixin` (memo and warm-before-write) plus forecast, data and day 2-7 sensors that borrow each other's methods by assignment |
| `__init__.async_setup_entry` | 474 lines, 10 closures | STPASA fan-out, refit gating, publish-time scheduling, force-refit service, listener wiring |
| `coordinator.PD7DayCoordinator` | 405 lines, 16 attrs | fetching, stale fallback, notice fetching, and two derived caches (`stpasa_index`, `current_run_features`) |
| `forecast_chart.render_forecast_chart` | 490-line function | data shaping, axes, zones, labels, callouts, legend, save |

Symptoms that make this debt rather than size alone:

- **Borrowed methods.** `sensor.py:1029`, `sensor.py:1660` and `tariff_sensor.py:1002` assign one class's methods onto another. This is how #66 happened (a copy drifted), it is the source of the "Invalid self argument" family of mypy errors, and it hides which state a method really needs.
- **Type debt concentrated in the entity layer.** 55 mypy errors, 22 of them in `sensor.py`, 9 in `config_flow.py`, 5 in `calibration_store.py`; the pure modules are nearly clean.
- **Everything depends on `const`** (26 of 40 modules) **and `nem_time`** (15), and the coordinator reaches into 12 sibling modules. There is no layer rule, so an entity can and does import the engine, the store and the clients directly.
- **Routing by module-level function.** The PRCER extension (#172) had to wrap five library calls in module functions because pricing is not an injectable collaborator.

## 2. Target architecture

Three layers, enforced by an import contract in CI rather than by convention.

```
adapters   HA entities, config flow, services, setup      (may import everything below)
           NEMWEB/TradingIS/STPASA clients, HA storage
services   CalibrationService, CalibratedForecastProvider,  (imports domain; never homeassistant)
           RunFeaturesProvider, StpasaIndex, RefitPolicy
domain     calibration (fitters, serving gates), pricing    (imports nothing from the package but
           (TariffPricer, RetailPrice), nem_time, const      const and nem_time; never homeassistant)
```

How SOLID maps onto this code:

- **Single responsibility.** Each row of the table above splits along its "mixes" column. `CalibrationStore` becomes `ObservationRepository`, `ForecastHistoryRepository`, `CalibrationModelRepository`, `ForecastIngest`, `ActualRecorder` and `RefitService`, behind a thin façade kept until the last caller moves.
- **Open/closed.** Pricing becomes a `TariffPricer` protocol with `LibraryPricer` and `ExtensionPricer` implementations chosen per tariff, replacing the wrapper functions. `CalibrationResult.apply` becomes an ordered pipeline of `ServingGate` objects (below-domain, spike, horizon band, feature domain, sign flip, floor, band containment), each carrying the issue it guards; a new gate is a new class, not an edit to a 169-line body.
- **Liskov.** No method is shared by assignment. Shared behaviour moves to a collaborator the entities hold (`CalibratedForecastProvider`, `IntervalClock`, `UsageFee`) or to a real base class (`TariffEntityBase` for the import and export tariff sensors).
- **Interface segregation.** Entities depend on narrow protocols (`CalibratedSpotSource`, `TariffPricer`, `PriceDataSource`) instead of the coordinator and the store. Tests then need a ten-line fake, not the HA stub tree.
- **Dependency inversion.** `async_setup_entry` becomes a composition root that builds an `EntryRuntime` from factories; nothing below the adapter layer imports `homeassistant`.

## 3. Method: specification-driven, agent-implemented

One unit of work is one specification, one agent implementation, one pull request.

1. **Specify.** A spec in `docs/specs/NNN-name.md` from `docs/specs/TEMPLATE.md`: the responsibility being moved, the new interfaces as typed signatures, the invariants, the behaviour contract (which observable outputs must not change), explicit non-goals, the migration steps, and the acceptance list. The spec is written from the code, not from memory, and cites file and line for every behaviour it pins.
2. **Review the spec, not the code.** The maintainer approves or amends the spec. This is the design decision point; everything after it is checkable.
3. **Implement.** One agent per spec, in an isolated git worktree (the test rationalisation showed that parallel agents sharing one working tree and index collide), given the spec and the repository and nothing else. It may not change a test's assertions; it may add contract tests the spec lists.
4. **Verify.** A second agent reads the diff against the spec's acceptance list and reports each item met or unmet with evidence. The orchestrator then runs the gates below.
5. **Ship.** Squash-merge, then either release on its own or batch with the next spec. Behaviour defects found on the way (as #171 was) are filed as issues and fixed in their own PRs, never inside a refactor.

### Gates every refactor PR must pass

| Gate | Tool | Pass condition |
|---|---|---|
| Behaviour | golden-master harness (spec 000) | every entity's state and attributes byte-identical to the recorded snapshot, across all recorded runs |
| Tests | full suite | unchanged pass count except tests the spec adds; no assertion edited |
| Coverage | line-level coverage comparison (added by spec 000) | no source line executed before is unexecuted after |
| Types | mypy ratchet | error count strictly lower, and zero in any module the spec creates |
| Layers | import-linter contracts | no new violation; the spec's module moves to its target layer |
| Size | a small AST check in CI | no new function over 60 lines, no new class over 250 lines or 15 methods |
| Live | shadow deploy | after deploy, 24 hours of published values identical to the previous release on the live install |

## 4. Sequence

Ordered so each step makes the next safer, and so the riskiest numerical code moves only once the net under it is strongest.

| # | Spec | Moves | Risk | Unlocks |
|---|---|---|---|---|
| 000 | Golden-master harness | new `tests/golden/`: record real coordinator inputs (PD7DAY run, STPASA, observations, dispatch) and snapshot every entity; import-linter in report-only mode; size check in CI | none, additive | the behaviour gate for everything after |
| 001 | Tariff pricing | `TariffPricer`, `LibraryPricer`, `ExtensionPricer`, `RetailPrice` (GST split from #158), `TouWindows` into `pricing/`; wrapper functions removed | low: small, recently written, well tested | removes the #172 routing hack |
| 002 | Tariff entity base | `TariffEntityBase` holds boundary scheduling, usage fee, current period, availability; import and export sensors shrink to their pricing and attributes | low | deletes about 120 duplicated lines and the three borrowed methods |
| 003 | Calibrated forecast provider | memo, key and warm-before-write out of `CalibratedWriteMixin` and the borrowed methods into one collaborator owned by `EntryRuntime` | medium: the #35, #58, #61 and #135 races live here | most of the 22 `sensor.py` type errors |
| 004 | Calibration store split | repositories, ingest, actual recorder, refit service; `CalibrationStore` kept as façade | medium: persistence formats must round-trip unchanged | 005 and 006 |
| 005 | Serving gate pipeline | `CalibrationResult.apply` into ordered `ServingGate` classes; order and each gate's issue pinned by the spec | high: every published price passes through it | new gates without touching the others |
| 006 | Fitters | `fit` and `fit_ols_stage2` into `Stage1Fitter` and `Stage2Fitter` with explicit inputs | high: numerical; exact equality required on the golden master | a testable fit without the engine |
| 007 | Coordinator derived state | `StpasaIndex` and `RunFeaturesProvider` out of `PD7DayCoordinator`; notice fetching to its own service | medium: the publication-order invariant from the STPASA index tests | coordinator becomes fetch and fallback only |
| 008 | Composition root | `async_setup_entry` into `EntryRuntimeBuilder`, `StpasaFanout`, `RefitScheduler`, service registration | medium: lifecycle and unload (#101, #106, the 2026.9 unload fix) | import-linter moves from report to enforce |
| 009 | Forecast chart | `render_forecast_chart` into data shaping, axes and zones, and the existing label and callout planners | low business risk, slow tests | the chart sweeps can target units instead of whole renders |

Steps 001 and 002 fit in one release; 003 and 004 in the next; 005 and 006 each deserve their own release and a day of shadow comparison; 007 to 009 can batch.

Before 000: rebase and merge PR #169 (test rationalisation), because every later spec edits tests it has already consolidated.

## 5. Done means

- No class over 250 lines or 15 methods, no function over 60 lines, outside a short list the specs justify.
- No method shared by assignment anywhere in the package.
- `homeassistant` imported only from the adapter layer, enforced in CI.
- mypy clean in the domain and service layers, and the overall ratchet below 20.
- The golden master has held through every step: nothing a user sees has changed.

## 6. What this plan deliberately does not do

- Change behaviour. Findings go to issues.
- Rename entities or unique ids, or migrate stored data. Where a store's format is touched (spec 004), the spec requires byte-identical round trips of the existing files.
- Split `const.py` for its own sake. It is a wide dependency but not a god class; it moves only where a spec needs a constant closer to its one user.
