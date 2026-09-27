# Spec 001: Tariff pricing

Status: approved 27 September 2026; drafted against `main` at 2d6a315 (v3.17.3)
Plan: docs/architecture/tech-debt-plan.md, step 001

## Responsibility

Converting a spot price into a network tariff price has one reason to change: the tariff rules themselves. That covers which source prices a code (the `aemo_to_tariff` library or the PRCER extension table), how the library's GST treatment is corrected (#158), and how time-of-use windows are parsed and matched. All of this lives today in `custom_components/nem_pd7day/tariff_sensor.py`, mixed in with Home Assistant entity code:

- module-level routing wrappers at `tariff_sensor.py:114-150`, added for #172 because pricing could not be injected;
- constants at `:73-111`;
- the GST split in `NemPd7dayTariffSensor._retail_price` (`:479-507`);
- window parsing and matching in `_tariff_windows` and `_lookup_period_info` (`:412-477`);
- period row normalisation in `_get_tariff_periods` (`:641-684`);
- the extension export windows in `NemPd7dayExportTariffSensor._get_tariff_periods` (`:1034-1056`).

This spec moves those rules into one domain-layer module, `tariff_pricing.py`, which does not import `homeassistant`. The entities keep everything else: caching, exception policy and logging, clock reads, and attribute building.

Amendment to the plan: the plan named a `pricing/` package. This spec uses a flat module instead, for two reasons. First, `tests/support.load` executes flat `<name>.py` files and would need a new loader path for a subpackage. Second, the import-linter layers in `.importlinter` are flat module lists.

## Current behaviour this must preserve

"Golden" means the golden-master snapshots in `tests/golden/snapshots/`. Every scenario covers the import, export and day 2-7 tariff sensors of each configured region.

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Import price calls the library as `spot_to_tariff(interval_end, distributor, code, rrp_mwh, dlf=1.05905, mlf=1.0154, market=1.0154)`, with the interval END as the first positional argument and $/MWh as the fourth | `tariff_sensor.py:121-125`, `:538-542`, `:588-592` | `test_tariff_sensor.py` lines 207, 636, 734, 752, 801; golden |
| Feed-in price calls the library as `spot_to_feed_in_tariff(interval_end, distributor, code, rrp_mwh)` with **no** loss factor arguments, so the library's own defaults apply | `:139`, `:1092-1094`, `:1123-1125` | `test_export_tariff.py` lines 117, 206, 290, 304, 353; golden |
| An extension feed-in price gets the three default loss factors explicitly (#174) | `:129-138` | `test_tariff_extensions.py`; golden (VIC1 PRCER scenario) |
| A code is routed to the extension exactly when `tariff_catalogue.priced_by_extension(distributor, code, export=...)` is true | `:114-150` | `test_tariff_extensions.py`, `test_tariff_catalogue.py` |
| `tariff_source` attribute is `"nem_pd7day extension"` or `"aemo-to-tariff"` | `:114-118`, `:790`, `:890`, `:1210` | golden |
| Import retail price, in order: `spot_c = rrp_mwh * DLF * MLF * MARKET / 10`, then `network_c = lib_c - spot_c`, then divide `network_c` by 1.1 when the distributor is in the `_LIB_APPLIES_GST` set, then `round(((spot_c + network_c) / 100 + fee) * 1.1, 6)`. The float operation order is part of the contract | `:503-507` | `test_tariff_gst.py` (probes the installed library); `support.expected_import_price`; golden |
| `_LIB_APPLIES_GST` is exactly `{energex, ergon, ausgrid, endeavour, essential, evoenergy, sapn}` | `:104-106` | `test_tariff_gst.py::test_library_gst_grouping_matches_constant` and `test_every_catalogue_distributor_is_classified` |
| Export price is `round(lib_c / 100, 6)`, with no fee and no GST | `:1095`, `:1126` | `test_export_tariff.py`; golden |
| Library stdout is suppressed around every library call. `get_periods` output is consumed (`list(...)`) inside the suppression | `:50-57`, `:536`, `:586`, `:651-652`, `:692-693`, `:1091`, `:1122` | `test_tariff_sensor.py` (noisy library test, around line 280); `test_export_tariff.py` around line 134 |
| When `aemo_to_tariff` is not importable, `_compute_tariff`, `_apply_tariff_to_spot`, `_compute_export_tariff`, `_apply_export_tariff_to_spot`, the import `_get_tariff_periods` and `_get_daily_supply_charge` return `None` or `[]`. This holds for extension codes too. The extension export windows do not depend on the library | `:60-70`, `:520`, `:565`, `:643`, `:688`, `:1076`, `:1106` | not pinned by a test today; see Acceptance |
| Extension periods are evaluated at `now_nem()` at the call; the library's periods take no time | `:140-143`, `:1054` | `test_tariff_extensions.py` |
| Period rows: 4-tuples or 5-tuples, with the name, start and end first and the rate in c/kWh last. Rows shorter than 4, or with a `None` rate, a `None` start or a `None` end, are skipped. Each kept row becomes `{"period", "start": "%H:%M", "end": "%H:%M", "network_rate_$/kwh": round(rate_c / 100, 6)}` | `:653-680` | `test_tariff_sensor.py` around line 420 (untimed row skipped, no "get_periods failed" log); golden `tariff_periods` |
| `ValueError` from `get_periods` yields `[]` silently. Any other exception yields `[]` with a debug log "get_periods failed for %s/%s" | `:681-690` | `test_tariff_sensor.py` line 407 and around line 430 |
| Extension export windows: `{"period", "start", "end", "export_adjustment_$/kwh": round(rate / 100, 6)}` from `tariff_extensions.feed_in_periods_for(distributor, code, now_nem())`; a library export returns `[]` | `:1034-1056` | `test_tariff_extensions.py`; golden `export_periods` |
| Daily supply charge: the library or extension `get_daily_fee`, with any exception giving `None` | `:686-696` | `test_tariff_sensor.py` lines 583-622; golden `daily_supply_charge_$` (value in c/day, see #171) |
| TOU lookup: the time of day of `parse_iso(nemtime) - 5 min`, matched first-wins against `[start, end)`, with wraparound when `start > end`; no match gives `(None, None)` | `:463-467` | `test_tariff_sensor.py` parametrised lookup tests around line 437; golden `period` and `network_rate` |
| Windows are parsed once per period list, keyed on the identity of the list. They are parsed through `datetime.datetime.strptime` looked up at call time (a test patches `datetime.datetime`). A parse failure is not caught during parsing and leaves `_cached_tariff_windows` unset | `:412-443` | `test_tariff_sensor.py` lines 485-559 |
| Loss factor attributes `distribution_loss_factor_dlf` 1.05905, `metering_loss_factor_mlf` 1.0154, `market_loss_factor` 1.0154, `combined_loss_multiplier` `round(DLF * MLF * MARKET, 6)`, `gst_multiplier` 1.1 | `:764-789`, `:869-889`, `:1192-1209` | golden |
| Price caches: keys include the fee on import (#181) and exclude it on export, with the dispatch path keyed on the ISO interval end | `:531-535`, `:579-583`, `:1085-1088`, `:1116-1119` | `test_tariff_sensor.py` lines 763-782, 873; `test_export_tariff.py` lines 315-334 |

## Interfaces

New module `custom_components/nem_pd7day/tariff_pricing.py`, domain layer. It may import only `tariff_catalogue`, `tariff_extensions` and the standard library, plus `aemo_to_tariff` behind the existing try/except. It must not import `nem_time`, because callers pass the time in, and it must not import `homeassistant`.

```python
import datetime  # module import: datetime.datetime is looked up at call time

DEFAULT_DLF: Final[float] = 1.05905
DEFAULT_MLF: Final[float] = 1.0154
DEFAULT_MARKET: Final[float] = 1.0154
COMBINED_LOSS_MULTIPLIER: Final[float]  # round(DEFAULT_DLF * DEFAULT_MLF * DEFAULT_MARKET, 6)
GST: Final[float] = 1.1
LIB_APPLIES_GST: Final[frozenset[str]]  # the seven distributors; the #158 comment moves with it

# aemo_to_tariff names, bound by the existing try/except, None when absent.
# Tests patch these on this module.
spot_to_tariff, spot_to_feed_in_tariff, get_periods, get_daily_fee

def library_available() -> bool: ...          # all four library names bound

@contextlib.contextmanager
def quiet_stdout() -> Iterator[None]: ...     # the current _suppress_stdout, unchanged

PeriodRow = tuple[Any, ...]                   # library row: (name, start, end, [condition,] rate_c)
FeedInRow = tuple[str, datetime.time, datetime.time, float]

class TariffPricer(Protocol):
    distributor: str
    code: str
    source: str  # "aemo-to-tariff" | "nem_pd7day extension"
    def import_c_kwh(self, interval_end: datetime.datetime, rrp_mwh: float) -> float: ...
    def feed_in_c_kwh(self, interval_end: datetime.datetime, rrp_mwh: float) -> float: ...
    def period_rows(self, now: datetime.datetime) -> list[PeriodRow]: ...
    def feed_in_rows(self, now: datetime.datetime) -> list[FeedInRow]: ...
    def daily_fee(self) -> float | None: ...

@dataclass(frozen=True)
class LibraryPricer:      # implements TariffPricer; every call inside quiet_stdout()
    distributor: str
    code: str
    source: str = "aemo-to-tariff"

@dataclass(frozen=True)
class ExtensionPricer:    # implements TariffPricer over tariff_extensions
    distributor: str
    code: str
    source: str = "nem_pd7day extension"

def pricer_for(distributor: str, code: str, *, export: bool = False) -> TariffPricer: ...

@dataclass(frozen=True)
class RetailPrice:
    distributor: str
    def import_dollars(self, library_c_kwh: float, rrp_mwh: float, fee: float) -> float: ...
    @staticmethod
    def export_dollars(library_c_kwh: float) -> float: ...

def period_attributes(rows: Iterable[PeriodRow]) -> list[dict[str, Any]]: ...
def feed_in_period_attributes(rows: Iterable[FeedInRow]) -> list[dict[str, Any]]: ...

@dataclass(frozen=True)
class TouWindows:
    windows: tuple[tuple[datetime.time, datetime.time, str | None, float | None], ...]
    @classmethod
    def parse(cls, periods: Sequence[Mapping[str, Any]]) -> TouWindows: ...  # raises on malformed
    def lookup(self, interval_end: datetime.datetime) -> tuple[str | None, float | None]: ...
```

Semantics each method must have:

- `LibraryPricer.import_c_kwh` passes the three defaults explicitly. `LibraryPricer.feed_in_c_kwh` passes none. `period_rows` returns `list(get_periods(distributor, code))` built inside `quiet_stdout()` and ignores `now`. `feed_in_rows` returns `[]`.
- `ExtensionPricer.feed_in_c_kwh` passes the three defaults explicitly. `period_rows(now)` is `tariff_extensions.get_periods(distributor, code, now)`. `feed_in_rows(now)` is `tariff_extensions.feed_in_periods_for(distributor, code, now)`.
- Pricers raise whatever the underlying call raises. Exception policy stays with the caller.
- `pricer_for(d, c, export=e)` returns an `ExtensionPricer` exactly when `priced_by_extension(d, c, export=e)` is true, and a `LibraryPricer` otherwise.
- `RetailPrice.import_dollars` is the `:503-507` body, verbatim in operation order. `export_dollars` is `round(c / 100, 6)`.
- `period_attributes` holds the `:653-680` loop and `feed_in_period_attributes` holds the `:1045-1056` comprehension.
- `TouWindows.lookup` subtracts five minutes and matches as at `:463-467`.

Changes to `tariff_sensor.py`:

- It does `from . import tariff_pricing` and calls through that name. It does not use `from .tariff_pricing import spot_to_tariff`, because that would bind a copy which test patches cannot reach.
- The module-level `_spot_to_tariff`, `_spot_to_feed_in_tariff`, `_get_periods`, `_get_daily_fee`, `_tariff_source`, `_suppress_stdout`, the aemo_to_tariff import block, `_DEFAULT_*`, `GST`, `_LIB_APPLIES_GST`, `_DISTRIBUTOR_LIB_MAP` (dead today) and `_att` (dead today) are deleted.
- Each entity method keeps its guard, cache, `try`/`except`, debug log and clock read. It replaces only the priced call and the arithmetic, as follows.

| Entity method | After |
|---|---|
| `_compute_tariff`, `_apply_tariff_to_spot` | guard `tariff_pricing.library_available()`; `pricer_for(...).import_c_kwh(...)`; `self._retail_price(...)` |
| `_retail_price` | stays as a one-line delegation to `RetailPrice(self._distributor).import_dollars`, because `test_tariff_gst.py:283` calls it |
| `_compute_export_tariff`, `_apply_export_tariff_to_spot` | guard as above; `pricer_for(..., export=True).feed_in_c_kwh(...)`; `RetailPrice.export_dollars` |
| import `_get_tariff_periods` | guard as above; `period_attributes(pricer_for(...).period_rows(now_nem()))`; `ValueError` and other exceptions handled exactly as today |
| export `_get_tariff_periods` | `feed_in_period_attributes(pricer_for(..., export=True).feed_in_rows(now_nem()))` for an extension code, `[]` otherwise, with no library guard (as today) |
| `_get_daily_supply_charge` | guard as above; `pricer_for(...).daily_fee()`; any exception gives `None` |
| `_tariff_windows` | keeps the identity cache on `_cached_tariff_windows`, now holding `(periods, TouWindows.parse(periods))` |
| `_lookup_period_info` | keeps `_MISSING`, the `try`/`except` and the debug log; computes `parse_iso(period.nemtime)` and returns `windows.lookup(...)` |
| attribute builders | read `tariff_pricing.DEFAULT_*`, `COMBINED_LOSS_MULTIPLIER` and `GST`; `tariff_source` is `pricer_for(...).source` |

Also in `tariff_sensor.py`: `_tariff_periods_for_attrs` gets a typed sentinel or a `cast`, so `tariff_sensor.py:639` stops being a mypy error.

## Invariants

1. For every distributor and code in the catalogue, with `aemo_to_tariff` installed, `RetailPrice(d).import_dollars(lib_c, rrp, fee)` returns the same float as the old `_retail_price`, bit for bit. A contract test compares the two expressions over a grid of prices (including negative, zero and 17,500 $/MWh) and fees, with `==`.
2. `pricer_for` makes the same decision as `priced_by_extension` for every catalogue import and export code.
3. `tariff_pricing` imports neither `homeassistant` nor `nem_time`. The import-linter domain layer lists it.
4. Every library call made through a `LibraryPricer` happens inside `quiet_stdout()`, and generators are consumed inside it.
5. The entities keep every cache key, guard and log message named in the table above, unchanged.
6. No published value moves: the golden master is identical.

## Migration

Each step leaves the suite green.

1. Add `tariff_pricing.py` beside the old code. Add `tests/test_tariff_pricing.py` with the contract tests:
   - invariant 1;
   - invariant 2;
   - the library call signatures (import with loss factors, feed-in without);
   - extension feed-in with the defaults;
   - `period_attributes` on 4-tuples, 5-tuples and the skipped rows;
   - `TouWindows.lookup` including wraparound and no match;
   - `library_available()` false when the four names are `None`;
   - stdout suppression.

   Add `tariff_pricing` to the domain layer in `.importlinter`.
2. Point `NemPd7dayTariffSensor` at it: pricing calls, `_retail_price`, periods, daily fee, windows, and attributes. In the same commit, retarget the test seams listed below. The suite and golden stay green.
3. Point `NemPd7dayExportTariffSensor` and `TariffForecastDays27Sensor` at it.
4. Delete the module-level wrappers and constants from `tariff_sensor.py`. Fix the `:639` type error.
5. Update `scripts/size_baseline.json` only by `--update`, which can only tighten. Regenerate `docs/architecture/import-report-000.txt` if the report changes.

### Test seam retargeting (the only permitted edits to existing tests)

Each existing test keeps its assertions. The only permitted edits change where a patch lands or where a constant is read from. Each edit is value-identical, and each is listed here so the verifier can check the diff mechanically. Every other hunk in an existing test file is a violation.

| In | From | To | Sites |
|---|---|---|---|
| `test_tariff_sensor.py`, `test_export_tariff.py`, `test_sensor.py`, `test_calibration_memo.py`, `test_tariff_calibration_parity.py` | `patch.object(_tariff_mod, "<name>"` for `spot_to_tariff`, `spot_to_feed_in_tariff`, `get_periods`, `get_daily_fee` (including the parametrised `library` variable) | `patch.object(_tariff_mod.tariff_pricing, "<name>"` | 52 |
| `test_tariff_gst.py:54-58` | `_tariff_mod.GST`, `._DEFAULT_DLF`, `._DEFAULT_MLF`, `._DEFAULT_MARKET`, `._LIB_APPLIES_GST` | `_tariff_mod.tariff_pricing.GST`, `.DEFAULT_DLF`, `.DEFAULT_MLF`, `.DEFAULT_MARKET`, `.LIB_APPLIES_GST` | 5 |
| `test_tariff_gst.py` failure message at line 245 | "in tariff_sensor.py" | "in tariff_pricing.py" | 1 |
| `test_tariff_extensions.py:169`, `:188` | `_tariff_mod._DEFAULT_*` | `_tariff_mod.tariff_pricing.DEFAULT_*` | 2 |
| `support.expected_import_price` | `tm._DEFAULT_*`, `tm._LIB_APPLIES_GST`, `tm.GST` | the same names via `tm.tariff_pricing` | 1 function |

Test files bind the pricing module through the tariff sensor module they loaded (`_tariff_mod.tariff_pricing`), never through their own `load("tariff_pricing")`. Each test file loads a fresh copy of the modules, and only the copy `tariff_sensor` bound is the one a patch must reach. A new test file that needs `tariff_pricing` on its own loads it with `load_chain(..., "tariff_catalogue", "tariff_extensions", "tariff_pricing")`.

## Non-goals

- #171 (`daily_supply_charge_$` published in c/day under a $ name): the value passes through unchanged. It is fixed in its own PR.
- The library feed-in call still relies on the library's default loss factors, while the import call passes them explicitly. That is inconsistent but identical in value today. Changing it is a behaviour question for an issue, not this spec.
- The library-missing guard also disables extension import pricing. This is preserved as is.
- Moving caches, boundary scheduling, the usage fee and `_current_period` into a base class is spec 002. The three borrowed calibration methods are spec 002 and 003. The mypy errors at `tariff_sensor.py:1079`, `:1167` and `:1171` stay.
- `tariff_catalogue.py` keeps its own `redirect_stdout` suppression. Merging the two is not required.
- No change to entity ids, names, unique ids or attribute keys.

## Acceptance

- [ ] Golden master identical for every recorded run, with no file under `tests/golden/snapshots/` changed, which the `refactor:` gate enforces.
- [ ] Full suite passes. The only edits to existing tests are the seam retargets tabled above. New tests are in `tests/test_tariff_pricing.py`, covering invariants 1 and 2, the call signatures, row normalisation, window lookup, `library_available` and stdout suppression.
- [ ] Coverage gate passes: no source line executed before is unexecuted after.
- [ ] mypy drops from 59 to 58 (`tariff_sensor.py:639`), with zero errors in `tariff_pricing.py`.
- [ ] Import contracts: `tariff_pricing` is in the domain layer, there is no new violation, and the report shows no `homeassistant` or `nem_time` import from it.
- [ ] Size: `tariff_pricing.py` has no function over 60 lines and no class over 250 lines or 15 methods. The baseline entries for `NemPd7dayTariffSensor` and `NemPd7dayExportTariffSensor` shrink or stay, and none grows.
- [ ] No method shared by assignment introduced.
- [ ] `tariff_sensor.py` contains none of: `_spot_to_tariff`, `_spot_to_feed_in_tariff`, `_get_periods`, `_get_daily_fee`, `_tariff_source`, `_suppress_stdout`, `_DEFAULT_`, `_LIB_APPLIES_GST`, `_DISTRIBUTOR_LIB_MAP`, `_att`, `import aemo_to_tariff`, `from aemo_to_tariff`.
- [ ] `grep -n "aemo_to_tariff" custom_components/nem_pd7day/*.py` finds the library imported only in `tariff_pricing.py` and `tariff_catalogue.py`.
- [ ] A test pins the library-missing row of the behaviour table (every guarded method returns `None` or `[]` when `library_available()` is false). This covers lines that are unexecuted today.
- [ ] PR title starts with `refactor:`.

## Rollback

The change is a single squash commit with no stored data. Revert the commit, then release. Nothing persisted refers to the moved names.
