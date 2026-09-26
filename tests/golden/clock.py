"""FrozenClock: one instant, patched into every wall-clock read the harness can reach.

The golden master compares published values exactly, so every read of the
wall clock that can reach a published value has to return the scenario's
instant. Rather than keeping a hand-written list of call sites, this module
finds them: at import it parses every module of the integration (and of
``aemo_to_tariff``, whose ``get_periods`` and ``get_daily_fee`` read the clock
when the integration calls them without an interval) and classifies each read.

Kinds it knows how to freeze, and how:

  datetime.now / datetime.utcnow / datetime.today
      ``from datetime import datetime`` at module level. The module's
      ``datetime`` binding is replaced by a subclass whose clock methods
      return the instant and whose constructors hand back plain datetimes.
  datetime.datetime.now (and date, utcnow, today)
      ``import datetime`` at module level. The binding is replaced by a copy
      of the module whose ``datetime`` and ``date`` are the frozen classes.
  date.today
      ``from datetime import date``: the binding becomes a frozen date class.
  time.monotonic / time.time / time.perf_counter (and the _ns forms)
      ``import time``: the binding becomes a copy of the module with those
      functions frozen. ``from time import monotonic`` style bindings are
      replaced by the frozen function directly.
  dt_util.now / dt_util.utcnow
      ``from homeassistant.util import dt as dt_util``: the binding becomes a
      proxy with frozen ``now``/``utcnow`` that delegates everything else.
  now_nem
      ``from .nem_time import now_nem``: the binding is replaced by a frozen
      function. nem_time's own ``datetime`` is frozen too, so a call reaching
      the original function through a function-local import is frozen as well.
  <kind> (default argument)
      A clock function captured as a parameter default, which is evaluated at
      import and so escapes any binding patch (``clock=time.monotonic`` in
      pd7day_shared and nemweb_gate). The function's ``__defaults__`` /
      ``__kwdefaults__`` are patched.

A clock function referenced (not called) inside a function body is looked up
through the module binding at run time, so the binding patch covers it; such
references are listed with the kind suffix ``(reference)``.

Anything else is a read this module does not know how to freeze: a call such
as ``x.now()`` whose receiver it cannot resolve to a clock, a clock read whose
name is bound by an import inside a function (a binding patch cannot reach
it), or a clock function captured at module or class level. Constructing a
FrozenClock raises ``UnfreezableClockRead`` listing every such site, so a new
unfrozen read fails the golden master rather than slipping in silently.
"""
from __future__ import annotations

import ast
import datetime as _dt
import importlib.util
import os
import sys
import time as _time
import types
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from support import PKG, PKG_DIR

# Third-party packages scanned and frozen alongside the integration. Their
# reads reach published values: aemo_to_tariff picks the price year and the
# season from the wall clock when no interval time is passed, and the tariff
# sensors call get_periods and get_daily_fee without one.
THIRD_PARTY_PACKAGES = ("aemo_to_tariff",)

_DT_CLOCK_ATTRS = frozenset({"now", "utcnow", "today"})
_DATE_CLOCK_ATTRS = frozenset({"today"})
_TIME_CLOCK_FUNCS = frozenset({
    "time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns",
})
_DT_UTIL_CLOCK_ATTRS = frozenset({"now", "utcnow"})
# Attribute names that look like a clock read when the receiver is unknown.
_SUSPICIOUS_ATTRS = frozenset({
    "now", "utcnow", "today", "monotonic", "monotonic_ns", "perf_counter",
    "perf_counter_ns", "time_ns",
})

# Origins, as the scan records the target of a binding.
O_DATETIME_CLASS = "datetime.datetime"
O_DATE_CLASS = "datetime.date"
O_DATETIME_MODULE = "module:datetime"
O_TIME_MODULE = "module:time"
O_DT_UTIL = "homeassistant.util.dt"
O_NOW_NEM = "nem_time.now_nem"
_LOCAL_IMPORT_FREEZABLE = frozenset({O_NOW_NEM, O_DT_UTIL})


class UnfreezableClockRead(AssertionError):
    """A wall-clock read the FrozenClock does not know how to freeze."""


@dataclass(frozen=True)
class ClockRead:
    module: str       # "coordinator", or "aemo_to_tariff.energex"
    lineno: int
    kind: str         # e.g. "dt_util.utcnow", "time.monotonic (default argument)"
    binding: str      # module-level name the patch replaces
    origin: str       # what that name is bound to, see the O_* constants
    source: str


@dataclass(frozen=True)
class ScanResult:
    reads: tuple[ClockRead, ...]
    problems: tuple[str, ...]

    def counts_by_kind(self) -> dict[str, int]:
        return dict(sorted(Counter(r.kind for r in self.reads).items()))

    def modules(self) -> list[str]:
        return sorted({r.module for r in self.reads})


# ── The scan ──────────────────────────────────────────────────────────────────

def _import_origins(node: ast.AST) -> dict[str, str]:
    """Names bound by one Import/ImportFrom node, mapped to their origin."""
    out: dict[str, str] = {}
    if isinstance(node, ast.Import):
        for alias in node.names:
            target = alias.name
            bound = alias.asname or target.split(".")[0]
            if target == "datetime":
                out[bound] = O_DATETIME_MODULE
            elif target == "time":
                out[bound] = O_TIME_MODULE
            elif target == "homeassistant.util.dt" and alias.asname:
                out[bound] = O_DT_UTIL
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if node.level == 0 and module == "datetime":
                if alias.name == "datetime":
                    out[bound] = O_DATETIME_CLASS
                elif alias.name == "date":
                    out[bound] = O_DATE_CLASS
            elif node.level == 0 and module == "time" and alias.name in _TIME_CLOCK_FUNCS:
                out[bound] = f"time.{alias.name}"
            elif node.level == 0 and module == "homeassistant.util" and alias.name == "dt":
                out[bound] = O_DT_UTIL
            elif alias.name == "now_nem" and (
                (node.level == 1 and module == "nem_time")
                or module.endswith("nem_pd7day.nem_time")
            ):
                out[bound] = O_NOW_NEM
    return out


def _local_names(fn: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Names a function binds itself (arguments, assignments) and its local imports."""
    bound: set[str] = set()
    imports: dict[str, str] = {}
    args = fn.args  # type: ignore[attr-defined]
    for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
        if a is not None:
            bound.add(a.arg)
    stack = list(fn.body)  # type: ignore[attr-defined]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                bound.add(node.name)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            origins = _import_origins(node)
            imports.update(origins)
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    return bound, imports


class _Scanner(ast.NodeVisitor):
    def __init__(self, module: str, tree: ast.Module) -> None:
        self.module = module
        self.reads: list[ClockRead] = []
        self.problems: list[str] = []
        self.module_bindings: dict[str, str] = {}
        self._scopes: list[tuple[set[str], dict[str, str]]] = []
        self._defaults: set[int] = set()
        self._call_funcs: set[int] = set()
        self._collect_module_bindings(tree)

    def _collect_module_bindings(self, tree: ast.Module) -> None:
        stack: list[ast.AST] = list(tree.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self.module_bindings.update(_import_origins(node))
                continue
            stack.extend(ast.iter_child_nodes(node))

    # Scope handling ---------------------------------------------------------

    def _resolve(self, name: str) -> tuple[str | None, str]:
        """(origin, where) for a Name load: where is module, local-import or local."""
        for bound, imports in reversed(self._scopes):
            if name in imports:
                return imports[name], "local-import"
            if name in bound:
                return None, "local"
        origin = self.module_bindings.get(name)
        return origin, "module"

    def _visit_function(self, node: ast.AST) -> None:
        args = node.args  # type: ignore[attr-defined]
        for default in [*args.defaults, *[d for d in args.kw_defaults if d is not None]]:
            for sub in ast.walk(default):
                self._defaults.add(id(sub))
            self.visit(default)
        for dec in getattr(node, "decorator_list", []):
            self.visit(dec)
        self._scopes.append(_local_names(node))
        for stmt in node.body:  # type: ignore[attr-defined]
            self.visit(stmt)
        self._scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        args = node.args
        for default in [*args.defaults, *[d for d in args.kw_defaults if d is not None]]:
            self.visit(default)
        bound = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
        self._scopes.append((bound, {}))
        self.visit(node.body)
        self._scopes.pop()

    # Classification ---------------------------------------------------------

    def _classify(self, func: ast.AST) -> tuple[str, str, str, str] | None:
        """(kind, binding, origin, where) for a clock callable expression, else None."""
        if isinstance(func, ast.Name):
            origin, where = self._resolve(func.id)
            if origin == O_NOW_NEM:
                return "now_nem", func.id, origin, where
            if origin and origin.startswith("time."):
                return origin, func.id, origin, where
            return None
        if not isinstance(func, ast.Attribute):
            return None
        attr = func.attr
        base = func.value
        if isinstance(base, ast.Name):
            origin, where = self._resolve(base.id)
            if origin == O_DATETIME_CLASS and attr in _DT_CLOCK_ATTRS:
                return f"datetime.{attr}", base.id, origin, where
            if origin == O_DATE_CLASS and attr in _DATE_CLOCK_ATTRS:
                return f"date.{attr}", base.id, origin, where
            if origin == O_TIME_MODULE and attr in _TIME_CLOCK_FUNCS:
                return f"time.{attr}", base.id, origin, where
            if origin == O_DT_UTIL and attr in _DT_UTIL_CLOCK_ATTRS:
                return f"dt_util.{attr}", base.id, origin, where
            return None
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            origin, where = self._resolve(base.value.id)
            if origin == O_DATETIME_MODULE and base.attr == "datetime" and attr in _DT_CLOCK_ATTRS:
                return f"datetime.datetime.{attr}", base.value.id, origin, where
            if origin == O_DATETIME_MODULE and base.attr == "date" and attr in _DATE_CLOCK_ATTRS:
                return f"datetime.date.{attr}", base.value.id, origin, where
        return None

    def _record(self, node: ast.AST, kind: str, binding: str, origin: str, where: str) -> None:
        src = ast.unparse(node)
        site = f"{self.module}:{node.lineno}: {src}"  # type: ignore[attr-defined]
        if where == "local-import" and origin in _LOCAL_IMPORT_FREEZABLE:
            # Resolved through sys.modules at call time: now_nem from the
            # nem_time module (itself frozen), dt_util from the
            # homeassistant.util stub (patched for the duration).
            kind = f"{kind} (function-local import)"
            where = "function"
        if where == "local-import":
            self.problems.append(
                f"{site} reads the clock through a name imported inside a function; "
                "a module binding patch cannot reach it"
            )
            return
        if where == "local":
            self.problems.append(f"{site} reads the clock through a local name")
            return
        if where != "function" and not self._scopes and not kind.endswith("(default argument)"):
            self.problems.append(f"{site} runs at import time, before any patch")
            return
        self.reads.append(ClockRead(self.module, node.lineno, kind, binding, origin, src))  # type: ignore[attr-defined]

    def visit_Call(self, node: ast.Call) -> None:
        self._call_funcs.add(id(node.func))
        found = self._classify(node.func)
        if found is not None:
            self._record(node, *found)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _SUSPICIOUS_ATTRS:
            # Whatever the receiver is, a .now()/.today()/.monotonic() that
            # did not resolve to a known clock is a read this module cannot
            # vouch for.
            self.problems.append(
                f"{self.module}:{node.lineno}: {ast.unparse(node)} looks like a "
                "clock read of a kind FrozenClock does not know how to freeze"
            )
        self.generic_visit(node)

    def _visit_reference(self, node: ast.AST) -> None:
        if id(node) in self._call_funcs:
            return
        found = self._classify(node)
        if found is None:
            return
        kind, binding, origin, where = found
        if id(node) in self._defaults:
            self._record(node, f"{kind} (default argument)", binding, origin, where)
        elif self._scopes:
            self._record(node, f"{kind} (reference)", binding, origin, where)
        else:
            self.problems.append(
                f"{self.module}:{node.lineno}: {ast.unparse(node)} captures a clock "  # type: ignore[attr-defined]
                "function at import time"
            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._visit_reference(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._visit_reference(node)


def scan_source(module: str, source: str) -> tuple[list[ClockRead], list[str]]:
    """Clock reads and unfreezable sites in one module's source."""
    tree = ast.parse(source)
    scanner = _Scanner(module, tree)
    for stmt in tree.body:
        scanner.visit(stmt)
    return scanner.reads, scanner.problems


def _package_sources() -> Iterable[tuple[str, str]]:
    for fname in sorted(os.listdir(PKG_DIR)):
        if fname.endswith(".py"):
            with open(os.path.join(PKG_DIR, fname), encoding="utf-8") as f:
                yield fname[:-3], f.read()
    for pkg in THIRD_PARTY_PACKAGES:
        spec = importlib.util.find_spec(pkg)
        if spec is None or not spec.submodule_search_locations:
            continue
        root = list(spec.submodule_search_locations)[0]
        for fname in sorted(os.listdir(root)):
            if fname.endswith(".py"):
                name = pkg if fname == "__init__.py" else f"{pkg}.{fname[:-3]}"
                with open(os.path.join(root, fname), encoding="utf-8") as f:
                    yield name, f.read()


def scan_package() -> ScanResult:
    reads: list[ClockRead] = []
    problems: list[str] = []
    for module, source in _package_sources():
        r, p = scan_source(module, source)
        reads.extend(r)
        problems.extend(p)
    return ScanResult(tuple(reads), tuple(problems))


SCAN = scan_package()


def is_third_party(module: str) -> bool:
    return module.split(".")[0] in THIRD_PARTY_PACKAGES


# ── Frozen stand-ins ──────────────────────────────────────────────────────────

_REAL_DATETIME = _dt.datetime
_REAL_DATE = _dt.date


class _DatetimeMeta(type):
    def __instancecheck__(cls, obj: Any) -> bool:
        return isinstance(obj, _REAL_DATETIME)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, _REAL_DATETIME)


class _DateMeta(type):
    def __instancecheck__(cls, obj: Any) -> bool:
        return isinstance(obj, _REAL_DATE)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, _REAL_DATE)


def _frozen_datetime_class(clock: "FrozenClock") -> type:
    class FrozenDatetime(_REAL_DATETIME, metaclass=_DatetimeMeta):
        """datetime whose clock methods return the frozen instant.

        Every constructor hands back a plain datetime, so nothing downstream
        ever holds an instance of this class.
        """

        def __new__(cls, *args: Any, **kwargs: Any) -> Any:
            return _REAL_DATETIME(*args, **kwargs)

        @classmethod
        def now(cls, tz: _dt.tzinfo | None = None) -> _dt.datetime:
            return clock.now(tz)

        @classmethod
        def utcnow(cls) -> _dt.datetime:
            return clock.instant.astimezone(_dt.timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls) -> _dt.datetime:
            return clock.now(None)

        @classmethod
        def fromisoformat(cls, s: str) -> _dt.datetime:
            return _REAL_DATETIME.fromisoformat(s)

        @classmethod
        def strptime(cls, s: str, fmt: str) -> _dt.datetime:
            return _REAL_DATETIME.strptime(s, fmt)

        @classmethod
        def fromtimestamp(cls, t: float, tz: _dt.tzinfo | None = None) -> _dt.datetime:
            return _REAL_DATETIME.fromtimestamp(t, tz)

        @classmethod
        def combine(cls, date: Any, time: Any, tzinfo: Any = True) -> _dt.datetime:
            if tzinfo is True:
                return _REAL_DATETIME.combine(date, time)
            return _REAL_DATETIME.combine(date, time, tzinfo)

    return FrozenDatetime


def _frozen_date_class(clock: "FrozenClock") -> type:
    class FrozenDate(_REAL_DATE, metaclass=_DateMeta):
        def __new__(cls, *args: Any, **kwargs: Any) -> Any:
            return _REAL_DATE(*args, **kwargs)

        @classmethod
        def today(cls) -> _dt.date:
            return clock.now(None).date()

        @classmethod
        def fromisoformat(cls, s: str) -> _dt.date:
            return _REAL_DATE.fromisoformat(s)

    return FrozenDate


class _FrozenDtUtil:
    """Stand-in for ``homeassistant.util.dt`` with a frozen clock."""

    def __init__(self, clock: "FrozenClock", original: Any) -> None:
        self._clock = clock
        self._original = original

    def utcnow(self) -> _dt.datetime:
        return self._clock.instant.astimezone(_dt.timezone.utc)

    def now(self, time_zone: _dt.tzinfo | None = None) -> _dt.datetime:
        # Home Assistant answers in its configured zone; the harness models an
        # install configured for NEM time.
        return self._clock.instant.astimezone(time_zone or self._clock.nem_tz)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


# ── The clock ─────────────────────────────────────────────────────────────────

_NEM_TZ = _dt.timezone(_dt.timedelta(hours=10), name="AEST")


class FrozenClock:
    """Patch every scanned clock read in ``modules`` to return one instant.

    ``modules`` maps the scan's module names ("coordinator", ...,
    "aemo_to_tariff.energex") to the module objects to patch. Every module the
    scan found a read in must be present, so a harness that forgot to load a
    module fails here rather than running that module unfrozen.
    """

    def __init__(
        self,
        instant: _dt.datetime,
        modules: Mapping[str, types.ModuleType],
        scan: ScanResult = SCAN,
    ) -> None:
        if scan.problems:
            raise UnfreezableClockRead(
                "FrozenClock found wall-clock reads it does not know how to freeze:\n  "
                + "\n  ".join(scan.problems)
            )
        missing = [m for m in scan.modules() if m not in modules]
        if missing:
            raise UnfreezableClockRead(
                "FrozenClock was not handed these modules, which read the clock: "
                + ", ".join(missing)
            )
        if instant.tzinfo is None:
            raise ValueError("FrozenClock needs an aware instant")
        self.instant = instant
        self.nem_tz = _NEM_TZ
        self._modules = modules
        self._scan = scan
        self._saved: list[tuple[Any, str, Any]] = []
        self._frozen_datetime = _frozen_datetime_class(self)
        self._frozen_date = _frozen_date_class(self)
        self._frozen_time_funcs: dict[str, Callable[[], Any]] = {
            "time": self.timestamp,
            "monotonic": self.timestamp,
            "perf_counter": self.timestamp,
            "time_ns": lambda: int(self.timestamp() * 1e9),
            "monotonic_ns": lambda: int(self.timestamp() * 1e9),
            "perf_counter_ns": lambda: int(self.timestamp() * 1e9),
        }
        self.patched: Counter[str] = Counter()

    # Readings ----------------------------------------------------------------

    def set(self, instant: _dt.datetime) -> None:
        """Move the frozen instant (the stale scenario replays an earlier fetch)."""
        if instant.tzinfo is None:
            raise ValueError("FrozenClock needs an aware instant")
        self.instant = instant

    def now(self, tz: _dt.tzinfo | None) -> _dt.datetime:
        if tz is None:
            # A naive now() would be host local time; model NEM time instead.
            return self.instant.astimezone(self.nem_tz).replace(tzinfo=None)
        return self.instant.astimezone(tz)

    def timestamp(self) -> float:
        return self.instant.timestamp()

    # Replacements ------------------------------------------------------------

    def _datetime_module(self) -> types.ModuleType:
        proxy = types.ModuleType("datetime")
        proxy.__dict__.update(
            {k: v for k, v in _dt.__dict__.items() if not k.startswith("__")}
        )
        proxy.datetime = self._frozen_datetime  # type: ignore[attr-defined]
        proxy.date = self._frozen_date  # type: ignore[attr-defined]
        return proxy

    def _time_module(self) -> types.ModuleType:
        proxy = types.ModuleType("time")
        proxy.__dict__.update(
            {k: v for k, v in _time.__dict__.items() if not k.startswith("__")}
        )
        for name, fn in self._frozen_time_funcs.items():
            setattr(proxy, name, fn)
        return proxy

    def _frozen_now_nem(self, original: Any) -> Callable[[], _dt.datetime]:
        tz = getattr(original, "__globals__", {}).get("NEM_TZ", self.nem_tz)

        def now_nem() -> _dt.datetime:
            return self.instant.astimezone(tz)

        return now_nem

    def _replacement(self, origin: str, original: Any) -> Any:
        if origin == O_DATETIME_CLASS:
            return self._frozen_datetime
        if origin == O_DATE_CLASS:
            return self._frozen_date
        if origin == O_DATETIME_MODULE:
            return self._datetime_module()
        if origin == O_TIME_MODULE:
            return self._time_module()
        if origin == O_DT_UTIL:
            return _FrozenDtUtil(self, original)
        if origin == O_NOW_NEM:
            return self._frozen_now_nem(original)
        if origin.startswith("time.") and origin[5:] in self._frozen_time_funcs:
            return self._frozen_time_funcs[origin[5:]]
        raise UnfreezableClockRead(f"no replacement for origin {origin!r}")

    def _real_clock_callables(self) -> dict[int, Callable[[], Any]]:
        out: dict[int, Callable[[], Any]] = {}
        for name, fn in self._frozen_time_funcs.items():
            out[id(getattr(_time, name))] = fn
        return out

    def _patch_defaults(self, module: types.ModuleType) -> None:
        real = self._real_clock_callables()

        def functions() -> Iterable[types.FunctionType]:
            for obj in list(vars(module).values()):
                if isinstance(obj, types.FunctionType) and obj.__module__ == module.__name__:
                    yield obj
                elif isinstance(obj, type) and obj.__module__ == module.__name__:
                    for member in vars(obj).values():
                        fn = getattr(member, "__func__", member)
                        if isinstance(fn, types.FunctionType):
                            yield fn

        for fn in functions():
            if fn.__defaults__ and any(id(d) in real for d in fn.__defaults__):
                self._saved.append((fn, "__defaults__", fn.__defaults__))
                fn.__defaults__ = tuple(real.get(id(d), d) for d in fn.__defaults__)
            if fn.__kwdefaults__ and any(id(d) in real for d in fn.__kwdefaults__.values()):
                self._saved.append((fn, "__kwdefaults__", fn.__kwdefaults__))
                fn.__kwdefaults__ = {k: real.get(id(d), d) for k, d in fn.__kwdefaults__.items()}

    def _freeze_local_import(self, read: ClockRead) -> None:
        """A read whose name is imported inside the function, from sys.modules."""
        if read.origin == O_NOW_NEM:
            # The import resolves to sys.modules' nem_time, whose own datetime
            # binding this clock freezes, so it only has to be the same module.
            live = sys.modules.get(f"{PKG}.nem_time")
            if live is not self._modules.get("nem_time"):
                raise UnfreezableClockRead(
                    f"{read.module}:{read.lineno} imports now_nem at call time, but "
                    f"sys.modules['{PKG}.nem_time'] is not the nem_time this clock froze"
                )
            return
        if read.origin == O_DT_UTIL:
            util = sys.modules.get("homeassistant.util")
            if util is None:
                raise UnfreezableClockRead(
                    f"{read.module}:{read.lineno} imports dt_util at call time and no "
                    "homeassistant.util module is installed"
                )
            if not any(obj is util and name == "dt" for obj, name, _ in self._saved):
                original = getattr(util, "dt", None)
                self._saved.append((util, "dt", original))
                setattr(util, "dt", _FrozenDtUtil(self, original))
            return
        raise UnfreezableClockRead(f"no function-local freeze for {read}")

    # Context -------------------------------------------------------------------

    def __enter__(self) -> "FrozenClock":
        done: set[tuple[str, str]] = set()
        for read in self._scan.reads:
            module = self._modules[read.module]
            key = (read.module, read.binding)
            if read.kind.endswith("(function-local import)"):
                self._freeze_local_import(read)
                self.patched[read.kind] += 1
                continue
            if read.kind.endswith("(default argument)"):
                if (read.module, "__defaults__") not in done:
                    done.add((read.module, "__defaults__"))
                    self._patch_defaults(module)
                self.patched[read.kind] += 1
                continue
            if key not in done:
                done.add(key)
                original = getattr(module, read.binding)
                self._saved.append((module, read.binding, original))
                setattr(module, read.binding, self._replacement(read.origin, original))
            self.patched[read.kind] += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        for obj, name, original in reversed(self._saved):
            setattr(obj, name, original)
        self._saved.clear()
