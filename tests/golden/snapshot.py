"""Canonical serialisation of every entity's observable output, and a structured diff.

``snapshot(entities, scenario)`` reads each entity the way Home Assistant
would when it writes state, keyed by unique id. ``dumps`` writes the result
as canonical JSON: sorted keys, two-space indent, floats as their shortest
round-trip ``repr`` (what ``json`` writes), so equality is exact to the bit.
``diff(expected, actual)`` lists every difference as one line naming the
entity, the attribute path and both values.

Canonical form, beyond what JSON already fixes:

  * numpy scalars become Python ``int``/``float`` first;
  * NaN and the infinities become the strings "nan", "inf", "-inf" (JSON has
    no spelling for them and ``allow_nan`` is off);
  * datetimes, dates and times are ISO 8601, an aware datetime with its
    offset; timedeltas are "timedelta:<seconds>";
  * sets become lists sorted by their canonical JSON text, tuples become lists;
  * enums become their value;
  * a dataclass becomes its fields plus "__dataclass__": its class name;
  * anything else is an error, so a MagicMock or a new type cannot slip into a
    snapshot unnoticed.

A property that raises is recorded as {"__raises__": "<Type>: <message>"}.

Cameras. The 7-day forecast chart records what ``_build_forecast_data()``
hands the renderer and the SHA-256 of the PNG ``_render()`` produces, which is
only comparable on matplotlib ``PNG_MATPLOTLIB_VERSION``. The time-of-day and
calibration chart cameras record their entity fields only: rendering them costs
about a second each per scenario, and what they draw is already pinned through
the ToD sensor's slots and the calibration sensor's summary.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import hashlib
import json
import math
import numbers
from typing import Any, Iterable, Mapping, Sequence

from support import PKG

PNG_MATPLOTLIB_VERSION = "3.11.1"

# HA Entity defaults for the properties recorded, used when neither the
# integration's own classes nor an ``_attr_`` value supply one.
_ENTITY_FIELDS: tuple[tuple[str, Any], ...] = (
    ("name", None),
    ("icon", None),
    ("device_class", None),
    ("state_class", None),
    ("native_unit_of_measurement", None),
    ("suggested_display_precision", None),
    ("entity_category", None),
    ("entity_registry_enabled_default", True),
    ("has_entity_name", False),
    ("attribution", None),
    ("should_poll", True),
    ("device_info", None),
)
_NUMBER_FIELDS: tuple[tuple[str, Any], ...] = (
    ("native_min_value", None),
    ("native_max_value", None),
    ("native_step", None),
    ("mode", None),
)
_CAMERA_FIELDS: tuple[tuple[str, Any], ...] = (
    ("content_type", None),
    ("brand", None),
    ("model", None),
    ("is_streaming", False),
    ("supported_features", None),
)


# ── Canonical values ─────────────────────────────────────────────────────────

def _float(value: float) -> Any:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return value


def _sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def canonical(value: Any) -> Any:
    """``value`` as plain JSON types, per the rules in the module docstring."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, enum.Enum):
        return canonical(value.value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        return _float(float(value))
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return f"timedelta:{value.total_seconds()!r}"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = k if isinstance(k, str) else _sort_key(canonical(k))
            if key in out:
                raise ValueError(f"two keys canonicalise to {key!r}")
            out[key] = canonical(v)
        return out
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canonical(v) for v in value), key=_sort_key)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: canonical(getattr(value, f.name)) for f in dataclasses.fields(value)}
        fields["__dataclass__"] = type(value).__name__
        return fields
    tolist = getattr(value, "tolist", None)
    if callable(tolist) and type(value).__module__.startswith("numpy"):
        return canonical(tolist())
    raise TypeError(f"cannot canonicalise {type(value).__module__}.{type(value).__qualname__}: {value!r}")


def dumps(snap: Mapping[str, Any]) -> str:
    return json.dumps(snap, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


# ── Reading an entity the way HA does ────────────────────────────────────────

def _defined_by_integration(entity: Any, name: str) -> bool:
    for klass in type(entity).__mro__:
        if klass.__module__.startswith(PKG) and name in klass.__dict__:
            return True
    return False


def _read(entity: Any, name: str, default: Any) -> Any:
    """A property the integration defines, else ``_attr_<name>``, else HA's default."""
    try:
        if _defined_by_integration(entity, name):
            value = getattr(entity, name)
        else:
            value = getattr(entity, f"_attr_{name}", default)
        return canonical(value)
    except Exception as exc:  # noqa: BLE001 - a raising property is an observable
        return {"__raises__": f"{type(exc).__name__}: {exc}"}


def _available(entity: Any) -> Any:
    try:
        if _defined_by_integration(entity, "available"):
            return canonical(entity.available)
        base = getattr(entity, "_attr_available", True)
        coordinator = getattr(entity, "coordinator", None)
        if coordinator is not None:
            base = base and coordinator.last_update_success
        return canonical(base)
    except Exception as exc:  # noqa: BLE001
        return {"__raises__": f"{type(exc).__name__}: {exc}"}


def _png_digest(render: Any) -> Any:
    try:
        data = render()
    except Exception as exc:  # noqa: BLE001
        return {"__raises__": f"{type(exc).__name__}: {exc}"}
    if not data:
        return None
    return hashlib.sha256(data).hexdigest()


def entity_record(entity: Any, domain: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "platform": domain,
        "entity_id": getattr(entity, "entity_id", None),
        "unique_id": _read(entity, "unique_id", None),
        "available": _available(entity),
    }
    for name, default in _ENTITY_FIELDS:
        record[name] = _read(entity, name, default)
    record["unrecorded_attributes"] = canonical(
        frozenset(getattr(entity, "_unrecorded_attributes", frozenset()))
    )
    if domain == "binary_sensor":
        record["state"] = _read(entity, "is_on", None)
    elif domain in ("sensor", "number"):
        record["state"] = _read(entity, "native_value", None)
    if domain != "camera":
        record["extra_state_attributes"] = _read(entity, "extra_state_attributes", None)
    if domain == "number":
        for name, default in _NUMBER_FIELDS:
            record[name] = _read(entity, name, default)
    if domain == "camera":
        for name, default in _CAMERA_FIELDS:
            record[name] = _read(entity, name, default)
        build = getattr(entity, "_build_forecast_data", None)
        if build is not None:
            # First build of the entity's life: the prior-run spike set is empty.
            record["forecast_data"] = _read_call(build)
            # The render builds the forecast again, now against the spike set
            # the first build saved, which is what a second render of the same
            # run sees live.
            record["png_sha256"] = _png_digest(entity._render)
    return record


def _read_call(fn: Any) -> Any:
    try:
        return canonical(fn())
    except Exception as exc:  # noqa: BLE001
        return {"__raises__": f"{type(exc).__name__}: {exc}"}


def snapshot(entities: Sequence[Any], scenario: Any, domains: Mapping[int, str] | None = None) -> dict[str, Any]:
    """Every entity keyed by unique id, plus the scenario's name and instant."""
    out: dict[str, Any] = {}
    for entity in entities:
        domain = (domains or {}).get(id(entity)) or entity.entity_id.split(".", 1)[0]
        record = entity_record(entity, domain)
        uid = record["unique_id"]
        if not isinstance(uid, str) or uid in out:
            raise ValueError(f"entity {entity!r} has a missing or duplicate unique id {uid!r}")
        out[uid] = record
    return {
        "scenario": scenario.name,
        "frozen_at": scenario.now.isoformat(),
        "matplotlib_for_png": PNG_MATPLOTLIB_VERSION,
        "entity_count": len(out),
        "entities": out,
    }


# ── Diff ─────────────────────────────────────────────────────────────────────

def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _same_scalar(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _walk(uid: str, path: str, expected: Any, actual: Any, out: list[str]) -> None:
    here = path or "<entity>"
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            sub = f"{path}.{key}" if path else key
            if key not in actual:
                out.append(f"{uid} | {sub} | expected {_short(expected[key])} | actual <absent>")
            elif key not in expected:
                out.append(f"{uid} | {sub} | expected <absent> | actual {_short(actual[key])}")
            else:
                _walk(uid, sub, expected[key], actual[key], out)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            out.append(f"{uid} | {here} | expected {len(expected)} items | actual {len(actual)} items")
        for i, (e, a) in enumerate(zip(expected, actual)):
            _walk(uid, f"{path}[{i}]", e, a, out)
        return
    if not _same_scalar(expected, actual):
        out.append(f"{uid} | {here} | expected {_short(expected)} | actual {_short(actual)}")


def diff(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """One line per difference: ``<unique id> | <attribute path> | expected .. | actual ..``."""
    out: list[str] = []
    for key in sorted((set(expected) | set(actual)) - {"entities"}):
        _walk("<snapshot>", key, expected.get(key, "<absent>"), actual.get(key, "<absent>"), out)
    exp_entities = expected.get("entities", {})
    act_entities = actual.get("entities", {})
    for uid in sorted(set(exp_entities) | set(act_entities)):
        if uid not in act_entities:
            out.append(f"{uid} | <entity> | expected present | actual <absent>")
        elif uid not in exp_entities:
            out.append(f"{uid} | <entity> | expected <absent> | actual present")
        else:
            _walk(uid, "", exp_entities[uid], act_entities[uid], out)
    return out


def iter_forecast(record: Mapping[str, Any], key: str = "forecast") -> Iterable[Mapping[str, Any]]:
    """The forecast list of a snapshot record's attributes, or nothing."""
    attrs = record.get("extra_state_attributes") or {}
    return attrs.get(key) or []
