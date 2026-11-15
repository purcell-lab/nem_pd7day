"""Pre-dispatch's own stage 1 calibration: fitted, validated and served by band (#337 stage 3b).

Stage 3a records each kept pre-dispatch forecast against the TradingIS actual,
in PD7DAY's observation row format (``pd7day_forecast`` holds the pre-dispatch
price). This module fits those rows the way PD7DAY's stage 1 is fitted, and
reuses its code for it (``fitting``): the rolling window, the intervention and
spike exclusions, the decay weights, the MAX_OBS cap, and per bucket the
quantile lines and the isotonic map. Serving is the bucket's ``apply_all``, so
the below-domain answer (#123) and the top clamp are PD7DAY's too. There is no
stage 2 (#333 decision 1).

**Buckets.** The 3a thinning edges ``[0, 1, 3, 6, 12, 24, 40]`` h by solar time
of day: ``p00_01`` to ``p24_40``, the ``p`` keeping them apart from PD7DAY's
``h`` buckets. A horizon of 40 h or more is in ``p24_40``.

**Fallback.** A bucket with fewer than ``MIN_BAND_OBS`` rows, or rows on fewer
than ``MIN_BAND_DAYS`` days, serves the next shorter band at the same time of
day. Where no band serves, the caller falls back: PD7DAY's ``h24_48`` model
from 24 h (the interim, ``predispatch_calibration``), else passthrough.

**Validation.** Each refit first fits on all but the newest ``HOLDOUT_DAYS``
and serves that holdout by the band's own buckets, a thin bucket's periods by
what would serve them without the band (the interim from 24 h, else raw). A
band is admitted only if its MAE there beats raw pre-dispatch's, and at
``p24_40`` the interim's too where a PD7DAY model is given. A band not
admitted serves nothing, so its periods take the fallback above. The serving
fit is then made on every row.

Domain layer: no Home Assistant imports. ``now`` is passed in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Sequence

import math

from .calibration_engine import DECAY_LAMBDA, BucketModel, Observation, _iso_bucket_stats, _tod_label, _tod_label_solar
from .const import MAX_OBS, NEM_TZ
from .fitting import fit_stage1_bucket, training_count, window_observations
from .serving import SPIKE_THRESHOLD

PD_HORIZON_EDGES: tuple[float, ...] = (0.0, 1.0, 3.0, 6.0, 12.0, 24.0, 40.0)
PD_HORIZON_LABELS: tuple[str, ...] = ("p00_01", "p01_03", "p03_06", "p06_12", "p12_24", "p24_40")
INTERIM_BAND = "p24_40"

MIN_BAND_OBS = 200
MIN_BAND_DAYS = 5
HOLDOUT_DAYS = 7
# Fewer holdout rows than this in a band and it cannot be judged: not admitted.
MIN_HOLDOUT_OBS = 50
# 3b fits once the record reaches this far back (#337).
MIN_HISTORY_DAYS = 14.0

PD_SOURCE = "pd_isotonic"

# (raw $/kWh, horizon h, interval start) -> the interim's $/kWh, or None where it would not serve.
InterimFn = Callable[[float, float, datetime], Optional[float]]


def pd_horizon_label(horizon_hours: float) -> Optional[str]:
    """The band of a horizon; None before the run (a negative horizon)."""
    if horizon_hours < 0:
        return None
    for edge, label in zip(PD_HORIZON_EDGES[1:], PD_HORIZON_LABELS):
        if horizon_hours < edge:
            return label
    return PD_HORIZON_LABELS[-1]


def _solar_tod(start: datetime, region: str) -> str:
    local = start.astimezone(NEM_TZ)
    return _tod_label_solar(local, region, _tod_label(local.hour))


@dataclass
class _Bucket:
    model: BucketModel
    n: int
    days: int

    @property
    def usable(self) -> bool:
        return self.model.iso_model is not None and self.n >= MIN_BAND_OBS and self.days >= MIN_BAND_DAYS


@dataclass
class PdCalibration:
    """A fitted pre-dispatch stage 1: buckets, the bands admitted, and the holdout that admitted them."""

    region: str
    fitted_at: str
    total_observations: int
    buckets: dict[str, _Bucket] = field(default_factory=dict)
    admitted: frozenset[str] = frozenset()
    validation: dict[str, dict[str, Any]] = field(default_factory=dict)

    def bucket_for(self, horizon_hours: float, start: datetime, *, admitted_only: bool = True) -> Optional[tuple[str, _Bucket]]:
        """The bucket a period is served from: its band's, else the next shorter band's, at its time of day."""
        band = pd_horizon_label(horizon_hours)
        if band is None:
            return None
        tod = _solar_tod(start, self.region)
        for label in reversed(PD_HORIZON_LABELS[:PD_HORIZON_LABELS.index(band) + 1]):
            if admitted_only and label not in self.admitted:
                continue
            key = f"{label}__{tod}"
            bucket = self.buckets.get(key)
            if bucket is not None and bucket.usable:
                return key, bucket
        return None

    def apply(self, raw_kwh: float, horizon_hours: float, start: datetime) -> Optional[dict[str, Any]]:
        """The served result for one period ($/kWh), labelled ``pd_isotonic``; None where no band serves."""
        found = self.bucket_for(horizon_hours, start)
        if found is None:
            return None
        key, bucket = found
        result = dict(bucket.model.apply_all(raw_kwh))
        result["stage1_source"] = result.get("calibrated_source")
        result["calibrated_source"] = PD_SOURCE
        result["bucket"] = key
        return result

    def summary(self) -> dict[str, Any]:
        """The ``pd_calibration`` attribute: buckets in the shape of PD7DAY's ``summary.buckets``, and the holdout."""
        buckets: dict[str, Any] = {}
        for key, bucket in sorted(self.buckets.items()):
            if bucket.n == 0:
                continue
            entry: dict[str, Any] = {
                "n": bucket.n, "days": bucket.days, "usable": bucket.usable,
                "ols_a": round(bucket.model.ols.a, 4), "iso_n_steps": None, "x_min": None, "x_max": None,
                "compression_ratio": None, "iso_mae": None, "spot_010": None, "spot_020": None,
                "iso_top": None, "iso_bottom": None,
            }
            entry.update(_iso_bucket_stats(bucket.model.iso_model))
            buckets[key] = entry
        return {
            "fitted_at": self.fitted_at,
            "total_observations": self.total_observations,
            "admitted_bands": sorted(self.admitted, key=PD_HORIZON_LABELS.index),
            "holdout_days": HOLDOUT_DAYS,
            "validation": self.validation,
            "buckets": buckets,
        }


def _partition(
    windowed: Sequence[tuple[Observation, datetime]], region: str, now_nem: datetime,
) -> tuple[dict[str, list[tuple[float, float]]], dict[str, list[float]], dict[str, list[int]]]:
    """Training pairs, decay weights and day ordinals per ``p`` bucket, as fitting.partition_stage1."""
    pairs: dict[str, list[tuple[float, float]]] = {}
    weights: dict[str, list[float]] = {}
    days: dict[str, list[int]] = {}
    tod_cache: dict[datetime, str] = {}
    for obs, obs_dt in windowed:
        if obs.is_intervention or obs.actual_rrp >= SPIKE_THRESHOLD or obs.pd7day_forecast >= SPIKE_THRESHOLD:
            continue
        band = pd_horizon_label(obs.horizon_hours)
        if band is None:
            continue
        tod = tod_cache.get(obs_dt)
        if tod is None:
            tod = tod_cache[obs_dt] = _solar_tod(obs_dt, region)
        key = f"{band}__{tod}"
        local = obs_dt.astimezone(NEM_TZ)
        pairs.setdefault(key, []).append((obs.pd7day_forecast, obs.actual_rrp))
        weights.setdefault(key, []).append(
            math.exp(-DECAY_LAMBDA * max((now_nem - local).total_seconds() / 86400.0, 0.0)))
        days.setdefault(key, []).append(local.date().toordinal())
    for key, w in weights.items():
        if len(w) > MAX_OBS:  # the most recent MAX_OBS, as PD7DAY (#209)
            keep = sorted(sorted(range(len(w)), key=lambda i: (w[i], i))[-MAX_OBS:])
            pairs[key] = [pairs[key][i] for i in keep]
            weights[key] = [w[i] for i in keep]
            days[key] = [days[key][i] for i in keep]
    return pairs, weights, days


def _fit_buckets(windowed: Sequence[tuple[Observation, datetime]], region: str, now_nem: datetime) -> dict[str, _Bucket]:
    pairs, weights, days = _partition(windowed, region, now_nem)
    return {
        key: _Bucket(fit_stage1_bucket(key, rows, weights[key], days[key]), len(rows), len(set(days[key])))
        for key, rows in pairs.items()
    }


def _validate(
    calibration: PdCalibration, holdout: Sequence[tuple[Observation, datetime]], interim: Optional[InterimFn],
) -> dict[str, dict[str, Any]]:
    """MAE and bias by band on the holdout: raw, served by this fit's chain, and the interim from 24 h."""
    sums: dict[str, dict[str, Any]] = {}
    for obs, obs_dt in holdout:
        if obs.is_intervention or obs.actual_rrp >= SPIKE_THRESHOLD or obs.pd7day_forecast >= SPIKE_THRESHOLD:
            continue
        band = pd_horizon_label(obs.horizon_hours)
        if band is None:
            continue
        raw, actual = obs.pd7day_forecast, obs.actual_rrp
        # What serves the period without this band: the interim from 24 h, else raw.
        interim_value = interim(raw, obs.horizon_hours, obs_dt) if interim is not None else None
        baseline = raw if interim_value is None else interim_value
        # The band's own bucket only: a thin one is scored at the baseline, so the
        # score is this band's, not a shorter band's standing in for it.
        own = calibration.buckets.get(f"{band}__{_solar_tod(obs_dt, calibration.region)}")
        served = float(own.model.apply_all(raw)["calibrated"]) if own is not None and own.usable else baseline
        acc = sums.setdefault(band, {"n": 0, "raw": [0.0, 0.0], "pd": [0.0, 0.0], "interim": [0.0, 0.0], "n_interim": 0})
        acc["n"] += 1
        for name, value in (("raw", raw), ("pd", served)):
            acc[name][0] += abs(value - actual)
            acc[name][1] += value - actual
        if interim is not None and obs.horizon_hours >= 24:
            acc["interim"][0] += abs(baseline - actual)
            acc["interim"][1] += baseline - actual
            acc["n_interim"] += 1
    table: dict[str, dict[str, Any]] = {}
    for band in PD_HORIZON_LABELS:
        held = sums.get(band)
        if held is None:
            table[band] = {"n": 0, "admitted": False, "reason": "no holdout rows"}
            continue
        acc = held
        n = acc["n"]
        row: dict[str, Any] = {
            "n": n,
            "raw_mae": round(acc["raw"][0] / n, 6), "raw_bias": round(acc["raw"][1] / n, 6),
            "pd_mae": round(acc["pd"][0] / n, 6), "pd_bias": round(acc["pd"][1] / n, 6),
        }
        if acc["n_interim"]:
            ni = acc["n_interim"]
            row.update(interim_mae=round(acc["interim"][0] / ni, 6), interim_bias=round(acc["interim"][1] / ni, 6))
        if n < MIN_HOLDOUT_OBS:
            row.update(admitted=False, reason=f"fewer than {MIN_HOLDOUT_OBS} holdout rows")
        elif row["pd_mae"] >= row["raw_mae"]:
            row.update(admitted=False, reason="no better than raw")
        elif band == INTERIM_BAND and "interim_mae" in row and row["pd_mae"] >= row["interim_mae"]:
            row.update(admitted=False, reason="no better than the interim")
        else:
            row.update(admitted=True, reason=None)
        table[band] = row
    return table


def fit_pd_calibration(
    observations: Sequence[Observation], region: str, now: datetime, interim: Optional[InterimFn] = None,
) -> PdCalibration:
    """Fit, validate on the newest HOLDOUT_DAYS, and refit on every row; ``now`` aware.

    ``interim`` serves a period as the interim would (PD7DAY's h24_48), for the
    ``p24_40`` admission test; without it that band is judged against raw only.
    """
    now_utc = now.astimezone(NEM_TZ)
    windowed = window_observations(observations, now_utc)
    cut = now_utc - timedelta(days=HOLDOUT_DAYS)
    train = [(o, t) for o, t in windowed if t < cut]
    holdout = [(o, t) for o, t in windowed if t >= cut]
    trial = PdCalibration(region, cut.isoformat(), training_count(train), _fit_buckets(train, region, cut))
    validation = _validate(trial, holdout, interim)
    admitted = frozenset(band for band, row in validation.items() if row.get("admitted"))
    return PdCalibration(
        region, now_utc.isoformat(), training_count(windowed), _fit_buckets(windowed, region, now_utc),
        admitted, validation,
    )
