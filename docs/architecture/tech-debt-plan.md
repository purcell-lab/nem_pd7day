# Technical debt plan: decomposing the god-class modules

Status: proposed, 26 September 2026, against `main` at `1179345` (v3.17.0 plus the test rationalisation, #169). Every figure below was re-measured at that commit; the mypy figures come from the `typecheck` job of CI run 36243665544 on the same commit.

This plan breaks the integration's largest modules into small units with one reason to change each, without changing a single published value or entity id. Every step is written as a specification first, implemented by an agent against that specification, and accepted only when a fixed set of automated gates pass.

## 1. Where the debt is

Measured on `main`. "Methods" counts methods defined on the class; "attrs" counts instance attributes assigned.

| Unit | Size | What it mixes |
|---|---|---|
| `calibration_store.CalibrationStore` | 702 lines, 24 methods, 12 attrs | persistence of three stores, forecast ingest, actual recording, the STPASA feature map, refit orchestration, serving (`apply_to_price`, `apply_calibration`) including the spike-credibility annotation, diagnostics summary |
| `tariff_sensor.NemPd7dayTariffSensor` | 601 lines, 25 methods | HA entity lifecycle, interval-boundary scheduling, calibrated-spot memo, TOU window lookup, retail price and GST, library/extension routing, attribute building |
| `tariff_sensor.NemPd7dayExportTariffSensor` | 322 lines, 17 methods | the same, with about 120 lines copied from the import sensor and three methods borrowed from it by assignment |
| `calibration_engine` | 2,185 lines | `CalibrationEngine.fit` 157 lines, `CalibrationEngine.fit_ols_stage2` 224, `CalibrationResult.apply` 169 lines holding every stage 2 serving gate from #67 to #153 in one body, `apply_all` 104, `summary` 85 |
| `sensor.py` | 1,758 lines | `CalibratedWriteMixin` (memo and warm-before-write) plus forecast, data and day 2-7 sensors that borrow each other's methods by assignment; `async_setup_entry` 110 lines |
| `__init__.async_setup_entry` | 474 lines, 10 nested functions | STPASA fan-out, refit gating, publish-time scheduling, force-refit service, listener wiring |
| `coordinator.PD7DayCoordinator` | 405 lines, 16 attrs | fetching, stale fallback, notice fetching, and two derived caches (`stpasa_index`, `current_run_features`) |
| `forecast_chart.render_forecast_chart` | 490-line function | data shaping, axes, zones, labels, callouts, legend, save |
| NEMWEB clients and parsers | 9 functions of 77 to 153 lines | `market_notice_client._parse_notice_body` 153 and `fetch_new_notices` 137, `pd7day_client._parse_all_tables` 112 and `fetch_all` 77, `nemweb_retry.fetch_with_retry` 97, `dispatch_client.fetch_dispatch_prices` 84, two STPASA parsers of 77 and 80 |
| Secondary charts and statistics | 5 functions of 66 to 221 lines | `bias_chart.render_chart` 221, `camera._build_forecast_data` 110, `tod_stats.compute` 103 and `render_chart` 95, `iso_chart.render_iso_chart` 66 |

Across the package, 39 functions exceed 60 lines and 8 classes exceed 250 lines or 15 methods. The first eight rows are the god classes this plan exists for; the last two rows are long procedures rather than mixed responsibilities, and are listed so the size baseline in spec 000 and the "done" condition in section 5 agree with each other.

The two modules added by v3.17.0 (#179), `scarcity_premium` (122 lines) and `scarcity_sensor` (152 lines), are small and single-purpose. They add three mypy errors, all in `scarcity_sensor`.

Symptoms that make this debt rather than size alone:

- **Borrowed methods.** Eleven class-level assignments at three sites copy one class's methods onto another: three at `sensor.py:1029`, five at `sensor.py:1660` and three at `tariff_sensor.py:1002`. This is how #66 happened (a copy drifted), it is the source of the five "Invalid self argument" mypy errors, and it hides which state a method really needs. The same pattern also leaves duplicated bodies behind, for example the cheapest-window search at `sensor.py:797` and `sensor.py:1053`, which carry the same two type errors.
- **Type debt concentrated in the entity layer.** 59 mypy errors in 15 files, against a baseline of 60 in `mypy_baseline.txt`. `sensor.py` holds 22 of them, 13 because `CalibratedWriteMixin` uses attributes it never declares; `config_flow.py` has 9, `calibration_store.py` 5, `tariff_sensor.py` 4 (three from borrowed methods), `tod_stats.py` 4 and `scarcity_sensor.py` 3. The domain modules are close to clean: `calibration_engine` and `calibration_inputs` have 2 each.
- **Everything depends on `const`** (26 of 40 modules) **and `nem_time`** (15), and the coordinator reaches into 12 sibling modules. There is no layer rule, so an entity can and does import the engine, the store and the clients directly.
- **Routing by module-level function.** The PRCER extension (#172) had to route pricing through four module-level wrappers in `tariff_sensor` (`_spot_to_tariff`, `_spot_to_feed_in_tariff`, `_get_periods`, `_get_daily_fee`, selected by `_tariff_source`) plus a direct call to `tariff_extensions.feed_in_periods_for` at `tariff_sensor.py:1029`, because pricing is not an injectable collaborator.
- **Dead and stale references.** `SPIKE_COVARIATE_RAW_FLOOR` and `SPIKE_COVARIATE_CAP` are defined in `const.py` and executed nowhere; their only other mention is the `camera._build_forecast_data` docstring (`camera.py:405` to `408`), which still describes the gas and QNI gate with a covariate cap that #177 replaced. This is the same class of debt as the bypass constant v3.16.0 retired.

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

Third-party libraries are not restricted by the contract. The domain layer already depends on `numpy` and `astral` (`calibration_engine`) and on `aemo_to_tariff` (`tariff_catalogue`), and will keep doing so.

How SOLID maps onto this code:

- **Single responsibility.** Each row of the table above splits along its "mixes" column. `CalibrationStore` becomes `ObservationRepository`, `ForecastHistoryRepository`, `CalibrationModelRepository`, `ForecastIngest`, `ActualRecorder`, `RefitService` and `CalibratedPriceServer` (serving and the spike-credibility annotation), behind a thin façade kept until the last caller moves. `build_stpasa_feature_map` leaves for `RunFeaturesProvider` in spec 007; `summary_attributes` stays on the façade for the diagnostics sensor until spec 008.
- **Open/closed.** Pricing becomes a `TariffPricer` protocol with `LibraryPricer` and `ExtensionPricer` implementations chosen per tariff, replacing the wrapper functions. `CalibrationResult.apply` becomes an ordered pipeline of `ServingGate` objects (below-domain clip, OLS horizon band, feature domain, sign disagreement, market floor, band re-clamp), each carrying the issue it guards; a new gate is a new class, not an edit to a 169-line body.
- **Liskov.** No method is shared by assignment. Shared behaviour moves to a collaborator the entities hold (`CalibratedForecastProvider`, `IntervalClock`, `UsageFee`) or to a real base class (`TariffEntityBase` for the import and export tariff sensors).
- **Interface segregation.** Entities depend on narrow protocols (`CalibratedSpotSource`, `TariffPricer`, `PriceDataSource`) instead of the coordinator and the store. Tests then need a ten-line fake, not the HA stub tree.
- **Dependency inversion.** `async_setup_entry` becomes a composition root that builds an `EntryRuntime` from factories; nothing below the adapter layer imports `homeassistant`.

### Spike credibility is an annotation, not a serving gate

The spike-credibility decision does not live in `CalibrationResult.apply` and does not change any price. It is computed in `CalibrationStore.apply_to_price` (`calibration_store.py:684` to `697`), where it adds a `spike_credible` key only when the raw price reaches `SPIKE_THRESHOLD`, and it is then published through two deliberately different views:

- sensor attributes go through `sensor._published_spike_credible` (`sensor.py:246`), which reports `None` inside `SPIKE_COVARIATE_MIN_HORIZON_H`;
- the chart copies the unsuppressed flag (`camera.py:498`) and gates callouts on it (`camera.py:530`).

That split was chosen in v3.16.0 (#177): the analysis-driven change is applied at the published attribute and the chart is left untouched. Specs 004 and 005 must keep the annotation out of the gate pipeline and must pin both views separately, so that a refactor which "unifies" them fails the golden master.

## 3. Method: specification-driven, agent-implemented

One unit of work is one specification, one agent implementation, one pull request.

1. **Specify.** A spec in `docs/specs/NNN-name.md` from `docs/specs/TEMPLATE.md`: the responsibility being moved, the new interfaces as typed signatures, the invariants, the behaviour contract (which observable outputs must not change), explicit non-goals, the migration steps, and the acceptance list. The spec is written from the code, not from memory, and cites file and line for every behaviour it pins.
2. **Review the spec, not the code.** The maintainer approves or amends the spec. This is the design decision point; everything after it is checkable.
3. **Implement.** One agent per spec, in an isolated git worktree (the test rationalisation showed that parallel agents sharing one working tree and index collide), branched from current `main` and never from another spec's branch, given the spec and the repository and nothing else. It may not change a test's assertions; it may add contract tests the spec lists, and it may retarget patch and import paths that the spec lists by name (the suite has 118 `patch.object` and `monkeypatch.setattr` sites bound to today's module layout, and `tests/test_tariff_sensor.py` patches `tariff_sensor.spot_to_tariff`, `get_periods` and `get_daily_fee` directly).
4. **Verify.** A second agent reads the diff against the spec's acceptance list and reports each item met or unmet with evidence. The orchestrator then runs the gates below.
5. **Ship.** Squash-merge after the maintainer confirms. Releases are cut only when the maintainer asks, either for one spec or as a batch with the next. Behaviour defects found on the way (as #171 was) are filed as issues and fixed in their own PRs, never inside a refactor.

### Gates every refactor PR must pass

| Gate | Tool | Pass condition |
|---|---|---|
| Behaviour | golden-master harness (spec 000) | every entity's state and attributes byte-identical to the recorded snapshot, across all recorded runs |
| Tests | full suite | unchanged pass count except tests the spec adds; no assertion edited; any retargeted patch or import path is listed in the spec |
| Coverage | line-level coverage comparison (added by spec 000) | no source line executed before is unexecuted after, mapped through the spec's list of moved code |
| Types | mypy ratchet | count never rises; strictly lower when the spec's modules carry errors today; zero in any module the spec creates |
| Layers | import-linter contracts | no new violation; the spec's module moves to its target layer |
| Size | a small AST check in CI | no new function over 60 lines, no new class over 250 lines or 15 methods |
| Live | recorded replay after deploy | inputs exported from the live install at four instants over the 24 hours after deploy, replayed through the previous and the new release, give identical outputs; the error log shows nothing new from the integration |

The live gate replays rather than compares live values directly, because two consecutive days of published values differ for market reasons, and the previous release is no longer running once the new one is installed. It also has a known blind spot: a forecast mode that is not configured on the live install is not exercised by it. The v3.16.0 short-lead suppression was a case in point, verified by tests only because both live entries run in day 2-7 mode. Each spec says which scenarios cover the modes the live install does not run.

## 4. Sequence

Ordered so each step makes the next safer, and so the riskiest numerical code moves only once the net under it is strongest.

Before spec 000, one small housekeeping PR, outside the refactor rules because it is not a refactor: remove `SPIKE_COVARIATE_RAW_FLOOR` and `SPIKE_COVARIATE_CAP`, rewrite the stale `_build_forecast_data` docstring to describe the region-interconnector gate, and lower `mypy_baseline.txt` from 60 to 59 as the ratchet already asks. The golden master then records a baseline without dead names in it.

| # | Spec | Moves | Risk | Unlocks |
|---|---|---|---|---|
| 000 | Golden-master harness | new `tests/golden/`: record real coordinator inputs (PD7DAY run, STPASA, observations, dispatch) and snapshot every entity; import-linter in report-only mode; size check in CI | none, additive | the behaviour gate for everything after |
| 001 | Tariff pricing | `TariffPricer`, `LibraryPricer`, `ExtensionPricer`, `RetailPrice` (GST split from #158), `TouWindows` into `pricing/`; the four wrappers and `_tariff_source` removed, and the direct `feed_in_periods_for` call moved behind the pricer | low: small, recently written, well tested | removes the #172 routing hack |
| 002 | Tariff entity base | `TariffEntityBase` holds boundary scheduling, usage fee, current period, availability; import and export sensors shrink to their pricing and attributes | low | deletes about 120 duplicated lines, the three borrowed methods and their three type errors |
| 003 | Calibrated forecast provider | memo, key and warm-before-write out of `CalibratedWriteMixin` and the eight borrowed methods into one collaborator owned by `EntryRuntime` | medium: the #35, #58, #61 and #135 races live here | 15 of the 22 `sensor.py` type errors (13 mixin, 2 borrowed) |
| 004 | Calibration store split | repositories, ingest, actual recorder, refit service, `CalibratedPriceServer`; `CalibrationStore` kept as façade | medium: persistence formats must round-trip unchanged; the spike annotation and both of its published views must stay as they are | 005 and 006 |
| 005 | Serving gate pipeline | `CalibrationResult.apply` into ordered `ServingGate` classes; order and each gate's issue pinned by the spec; spike credibility stays an annotation outside the pipeline | high: every published price passes through it | new gates without touching the others |
| 006 | Fitters | `fit` and `fit_ols_stage2` into `Stage1Fitter` and `Stage2Fitter` with explicit inputs | high: numerical; exact equality required on the golden master | a testable fit without the engine |
| 007 | Coordinator derived state | `StpasaIndex` and `RunFeaturesProvider` out of `PD7DayCoordinator`, taking `build_stpasa_feature_map` from the store façade; notice fetching to its own service | medium: the publication-order invariant from the STPASA index tests | coordinator becomes fetch and fallback only |
| 008 | Composition root | `async_setup_entry` into `EntryRuntimeBuilder`, `StpasaFanout`, `RefitScheduler`, service registration; `sensor.async_setup_entry` into per-platform factories | medium: lifecycle and unload (#101, #106, the 2026.9 unload fix) | import-linter moves from report to enforce |
| 009 | Forecast chart | `render_forecast_chart` into data shaping, axes and zones, and the existing label and callout planners | low business risk, slow tests | the chart sweeps can target units instead of whole renders |
| 010 | NEMWEB parsers | the nine long client and parser functions into per-table parsers and a single retry policy | low, but outside the golden master, which starts from parsed results | parsers testable against recorded NEMWEB files one table at a time |
| 011 | Secondary charts and statistics | `bias_chart`, `iso_chart`, `tod_stats` and `camera._build_forecast_data` split into data shaping and rendering | low | the size baseline empties outside the justified list |

Steps 001 and 002 fit in one release; 003 and 004 in the next; 005 and 006 each deserve their own release and a day of recorded replay; 007 to 011 can batch.

Spec 010 needs a behaviour gate of its own, because the golden master begins after parsing. Its spec should first commit a small set of real NEMWEB sample files and pin the parsed output on them, since the existing parser tests mostly build their inputs inline, and only then move any function.

PR #169 (test rationalisation) is merged, so every spec starts from the consolidated suite and `tests/support.py`.

## 5. Done means

- No class over 250 lines or 15 methods, no function over 60 lines, outside a short list the specs justify. The candidates for that list today are the two `config_flow` steps (`async_step_reconfigure` 74 lines, `async_step_init` 75), which follow Home Assistant's flow idiom; the maintainer decides whether they stay.
- No method shared by assignment anywhere in the package.
- `homeassistant` imported only from the adapter layer, enforced in CI.
- mypy clean in the domain and service layers, and the overall ratchet below 20.
- The golden master has held through every step: nothing a user sees has changed.

## 6. What this plan deliberately does not do

- Change behaviour. Findings go to issues.
- Rename entities or unique ids, or migrate stored data. Where a store's format is touched (spec 004), the spec requires byte-identical round trips of the existing files.
- Split `const.py` for its own sake. It is a wide dependency but not a god class; it moves only where a spec needs a constant closer to its one user.
- Merge the sensor and chart views of spike credibility. They differ on purpose (section 2).
