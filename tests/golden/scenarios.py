"""The twelve synthetic golden-master scenarios.

Each scenario is a small, deterministic description of one config entry's
inputs at one frozen instant: the PD7DAY run the coordinator fetches, the
STPASA run in the region's store, the calibration state, dispatch prices,
market notices, the usage fee number's restored value and, for the stale
scenario, the failed fetch that made it stale.

Scenarios are built against the module objects the harness loaded (the same
rule as ``support``'s builders, see its docstring), so a builder takes a
``mods`` namespace and returns a ``Scenario`` whose dataclasses are that
chain's own. Every number comes from ``SyntheticMarket``, a fixed function of
the scenario's seed, the region and the timestamp: no global random state is
touched and nothing depends on the order scenarios run in.

Forecast layout. A real PD7DAY run is 336 half-hour intervals, and every
tariff entity of a region publishes all of them, which would make a snapshot
several megabytes. The synthetic runs are thinned instead: contiguous half
hours for the first 8 hours (so the current interval, the cheapest 2 h window
and the near-term covariates behave as they do live), then one interval every
2 hours to 36 hours, then every 6 hours to the end of day 7 on the long
layout. The first-24-hour min/max therefore spans more than 24 hours of
clock time on a thinned run; the code sizes that window in intervals. Every horizon band and time-of-day bucket the calibration serves is
still reached. The STPASA runs are not thinned.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

NEM = timezone(timedelta(hours=10))
UTC = timezone.utc
HALF_HOUR = timedelta(minutes=30)
PUBLISH_SLOTS = ((7, 30), (13, 0), (18, 0))


def nem(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=NEM)


def iso(dt: datetime) -> str:
    """ISO-8601 in NEM time with the explicit +10:00 suffix the integration writes."""
    return dt.astimezone(NEM).strftime("%Y-%m-%dT%H:%M:%S+10:00")


# ── Calibration seeds ─────────────────────────────────────────────────────────

class CalibrationSeed(Protocol):
    """How a scenario's calibration store is populated.

    ``payloads`` returns storage payloads keyed by Home Assistant storage key,
    written before any store is constructed. ``load_into`` then runs on the
    real CalibrationStore the harness built, exactly where __init__.py awaits
    ``store.async_load()``.
    """

    prefit: bool

    def payloads(self, mods: Any, market: "SyntheticMarket", now: datetime) -> dict[str, Any]: ...

    async def load_into(self, store: Any) -> None: ...


@dataclass(frozen=True)
class EmptySeed:
    """No calibration yet: nothing in storage, nothing to fit."""

    prefit: bool = False

    def payloads(self, mods: Any, market: "SyntheticMarket", now: datetime) -> dict[str, Any]:
        return {}

    async def load_into(self, store: Any) -> None:
        await store.async_load()


@dataclass(frozen=True)
class ObservationSeed:
    """Observations generated from a fixed seed, fitted with the real engine.

    ``payloads`` writes the observation log (daily segments and manifest) and
    the forecast history as the stores write them; ``load_into`` loads them.
    The harness then runs the startup refit exactly as __init__.py guards it
    (``store.async_refit()`` once there are ten observations), so the stage 1
    and stage 2 fits are part of what the snapshot pins.

    ``prefit=True`` has the harness first fit on a throwaway store over the
    same storage, an hour before setup, so the coefficient store holds a real
    earlier fit. The scenario's store then restores it through
    ``CalibrationEngine.from_storage`` before the startup refit replaces it,
    which is the order a restart takes; the coordinator's first refresh
    computes the time-of-day statistics from the restored fit in between.
    """

    days: int = 14
    step: int = 4
    prefit: bool = False

    def payloads(self, mods: Any, market: "SyntheticMarket", now: datetime) -> dict[str, Any]:
        const = mods.const
        rows = market.observations(mods, now, days=self.days, step=self.step)
        segments: dict[str, list[dict]] = {}
        for row in rows:
            segments.setdefault(row["interval_time"][:10], []).append(row)
        region = market.region
        out: dict[str, Any] = {
            const.observation_manifest_key(region): {"dates": sorted(segments)},
        }
        for day, day_rows in segments.items():
            out[const.observation_segment_key(region, day)] = {"observations": day_rows}
        _obs_key, _coeff_key, fh_key = const.storage_keys(region)
        out[fh_key] = {"forecast_history": market.forecast_history(mods, now, step=self.step)}
        return out

    async def load_into(self, store: Any) -> None:
        await store.async_load()


# ── Scenario ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StaleState:
    """The coordinator served ``first_fetch_at``'s run, then a fetch failed at ``now``."""

    first_fetch_at: datetime
    failure_status: int


@dataclass(frozen=True)
class Scenario:
    name: str
    region: str
    now: datetime                           # the frozen instant, NEM time
    options: Mapping[str, Any]              # config entry options
    pd7day: Any                             # PD7DayResult, all regions the fetch returns
    stpasa: Any | None                      # StpasaResult in the region's store
    stpasa_fetched_at: datetime | None
    calibration: CalibrationSeed
    market: "SyntheticMarket"
    dispatch: Mapping[str, Any] | None      # region -> DispatchPrice
    notices: Sequence[tuple[int, str]]      # (notice id, body) parsed by the real parser
    usage_fee: float | None                 # the number entity's restored state
    stale: StaleState | None = None
    scarcity_samples: Mapping[str, float] | None = None
    entry_id: str = ""


# ── The synthetic market ──────────────────────────────────────────────────────

# $/kWh by NEM hour: overnight shoulder, morning ramp, solar trough, evening peak.
_PROFILE = (
    0.085, 0.080, 0.078, 0.078, 0.082, 0.095, 0.120, 0.110, 0.075, 0.045, 0.025, 0.015,
    0.012, 0.018, 0.030, 0.060, 0.140, 0.260, 0.330, 0.240, 0.160, 0.120, 0.100, 0.090,
)
_REGION_SCALE = {"QLD1": 1.0, "NSW1": 1.05, "VIC1": 0.92, "SA1": 1.18, "TAS1": 0.85}
_DEMAND_MW = {"QLD1": 6400.0, "NSW1": 8200.0, "VIC1": 5600.0, "SA1": 1450.0, "TAS1": 1100.0}
_SOLAR_MW = {"QLD1": 3200.0, "NSW1": 2900.0, "VIC1": 1800.0, "SA1": 900.0, "TAS1": 150.0}
# id: (base flow, daily swing, exportlimit, importlimit), MW in the nominal direction.
_IC = {
    "NSW1-QLD1": (-350.0, 450.0, 1150.0, -1250.0),
    "N-Q-MNSP1": (-40.0, 60.0, 180.0, -200.0),
    "VIC1-NSW1": (400.0, 500.0, 1350.0, -1100.0),
    "V-SA": (150.0, 250.0, 650.0, -600.0),
    "V-S-MNSP1": (60.0, 100.0, 220.0, -200.0),
    "T-V-MNSP1": (200.0, 250.0, 478.0, -478.0),
}


def _unit(*key: Any) -> float:
    """A deterministic uniform draw in [0, 1), keyed by ``key``.

    A keyed hash rather than a seeded generator, so a value depends only on
    its key: nothing drawn earlier, and no global random state, can move it.
    """
    digest = hashlib.blake2b(":".join(str(k) for k in key).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2.0**64


# Every input below is built from +, -, *, / and comparisons only. Those are
# correctly rounded by IEEE 754 on every machine, whereas libm's sin, exp,
# tanh and log may differ by an ulp between C libraries, and a one-ulp input
# difference can flip a value published at six decimals. CI caught exactly
# that when the first version of this file used them.


def _gauss(*key: Any) -> float:
    """A deterministic, approximately standard normal draw keyed by ``key``.

    Irwin-Hall: the sum of twelve uniforms less six has mean 0 and variance 1.
    """
    return sum(_unit(*key, i) for i in range(12)) - 6.0


def _solar_frac(hour: float) -> float:
    """A daylight bump from 0 at 06:00 to 1 at midday to 0 at 18:30 (parabola)."""
    if hour <= 6.0 or hour >= 18.5:
        return 0.0
    x = (hour - 6.0) / 12.5
    return 4.0 * x * (1.0 - x)


def _evening(hour: float) -> float:
    """An evening bump peaking at 18:30 (a Cauchy-shaped rational function)."""
    z = (hour - 18.5) / 2.5
    return 1.0 / (1.0 + z * z)


def _diurnal(hour: float) -> float:
    """A triangle wave with the phase of sin(2 pi (hour - 6) / 24): 0 at 06:00, 1 at 12:00."""
    t = ((hour - 6.0) / 24.0) % 1.0
    if t < 0.25:
        return 4.0 * t
    if t < 0.75:
        return 2.0 - 4.0 * t
    return 4.0 * t - 4.0


@dataclass(frozen=True)
class SyntheticMarket:
    """Every synthetic number, as a pure function of (seed, region, time).

    ``price_overrides`` and ``ic_overrides`` pin the live run's values at named
    interval starts (a spike, a depressed link); ``stpasa_scale`` multiplies the
    live STPASA surplus over a window (the out-of-domain scenario). None of the
    overrides touch the training observations.
    """

    seed: int
    region: str
    price_overrides: Mapping[str, float] = field(default_factory=dict)
    ic_overrides: Mapping[tuple[str, str], Mapping[str, float]] = field(default_factory=dict)
    gas_overrides: Mapping[str, float] = field(default_factory=dict)
    stpasa_surplus_override: tuple[datetime, datetime, float] | None = None
    midday_depth: float = 0.035

    # Weather, demand and STPASA -------------------------------------------------

    def day_factor(self, day: date) -> float:
        return 0.85 + 0.3 * _unit(self.seed, self.region, "day", day.isoformat())

    def midday_dip(self, day: date) -> float:
        """How far below zero a sunny day pushes the midday price."""
        u = _unit(self.seed, self.region, "sun", day.isoformat())
        return self.midday_depth * u if u > 0.45 else 0.0

    def stpasa_mw(self, start: datetime) -> dict[str, float]:
        local = start.astimezone(NEM)
        hour = local.hour + local.minute / 60.0
        day = local.date()
        dem = _DEMAND_MW[self.region]
        solar_cap = _SOLAR_MW[self.region]
        d_rnd = _unit(self.seed, self.region, "demand", day.isoformat())
        s_rnd = _unit(self.seed, self.region, "cloud", day.isoformat())
        d50 = dem * (0.78 + 0.22 * _evening(hour) - 0.20 * _solar_frac(hour)) * (0.95 + 0.1 * d_rnd)
        solar = solar_cap * _solar_frac(hour) * (0.6 + 0.4 * s_rnd)
        wind = 0.2 * dem * (0.5 + 0.5 * (2.0 * _unit(self.seed, self.region, "wind", day.isoformat()) - 1.0))
        noise = 120.0 * _gauss(self.seed, self.region, "surplus", iso(start))
        surplus = dem * 1.45 - d50 * (1.0 + 0.3 * _evening(hour)) + 0.35 * solar + noise
        return {
            "demand10": round(d50 * 1.055, 3),
            "demand50": round(d50, 3),
            "demand90": round(d50 * 0.955, 3),
            "surpluscapacity": round(max(surplus, 50.0), 3),
            "ss_solar_uigf": round(solar, 3),
            "ss_wind_uigf": round(wind, 3),
        }

    def surplus_norm(self, start: datetime) -> float:
        dem = _DEMAND_MW[self.region]
        s = self.stpasa_mw(start)["surpluscapacity"]
        return min(1.0, max(0.0, (s - 0.15 * dem) / (0.45 * dem)))

    def gas_tj(self, day: date) -> float:
        key = day.isoformat()
        if key in self.gas_overrides:
            return self.gas_overrides[key]
        return round(110.0 + 30.0 * _unit(self.seed, "gas", key), 1)

    # Prices --------------------------------------------------------------------

    def true_price(self, start: datetime) -> float:
        local = start.astimezone(NEM)
        hour = local.hour
        nxt = _PROFILE[(hour + 1) % 24]
        base = _PROFILE[hour] + (nxt - _PROFILE[hour]) * (local.minute / 60.0)
        scale = _REGION_SCALE[self.region] * self.day_factor(local.date())
        tight = 1.25 - 0.5 * self.surplus_norm(start)
        price = base * scale * tight
        if 10 <= hour < 15:
            price -= self.midday_dip(local.date()) * _solar_frac(hour + local.minute / 60.0)
        return price + 0.006 * _gauss(self.seed, self.region, "true", iso(start))

    def forecast_price(self, start: datetime, run_at: datetime) -> float:
        key = iso(start)
        if key in self.price_overrides:
            return self.price_overrides[key]
        h = (start - run_at).total_seconds() / 3600.0
        r = h / 36.0
        bias = 1.0 + 0.10 * r / (1.0 + abs(r))
        sd = 0.003 + 0.00025 * h
        noise = sd * _gauss(self.seed, self.region, "fc", key, iso(run_at))
        return round(self.true_price(start) * bias + noise, 5)

    # Interconnectors -----------------------------------------------------------

    def ic_point(self, ic_id: str, start: datetime) -> dict[str, float]:
        base, swing, export_lim, import_lim = _IC[ic_id]
        local = start.astimezone(NEM)
        hour = local.hour + local.minute / 60.0
        flow = base + swing * _diurnal(hour)
        flow += 25.0 * _gauss(self.seed, ic_id, "flow", iso(start))
        point = {
            "mwflow": round(flow, 3),
            "meteredmwflow": round(flow * 0.98, 3),
            "mwlosses": round(abs(flow) * 0.021, 3),
            "marginalvalue": 0.0,
            "violationdegree": 0.0,
            "exportlimit": export_lim,
            "importlimit": import_lim,
            "marginalloss": 1.0,
        }
        point.update(self.ic_overrides.get((ic_id, iso(start)), {}))
        return point

    # Training data -------------------------------------------------------------

    @staticmethod
    def stpasa_coverage_start(stpasa_run: datetime) -> datetime:
        """First covered interval START: the next trading day's 04:00."""
        local = stpasa_run.astimezone(NEM)
        return datetime(local.year, local.month, local.day, 4, 0, tzinfo=NEM) + timedelta(days=1)

    def past_runs(self, now: datetime, days: int) -> list[datetime]:
        runs = []
        today = now.astimezone(NEM).date()
        for back in range(days, -1, -1):
            day = today - timedelta(days=back)
            for hh, mm in PUBLISH_SLOTS:
                run_at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=NEM)
                if run_at < now - HALF_HOUR:
                    runs.append(run_at)
        return runs

    def _stpasa_for_run(self, run_at: datetime, start: datetime) -> tuple[str, dict] | None:
        stpasa_run = run_at - timedelta(hours=1, minutes=30)
        cov = self.stpasa_coverage_start(stpasa_run)
        if not (cov <= start < cov + timedelta(days=6)):
            return None
        return iso(stpasa_run), self.stpasa_mw(start)

    def observations(self, mods: Any, now: datetime, *, days: int, step: int) -> list[dict]:
        """Settled (forecast, actual) pairs in the observation log's stored shape."""
        transform = mods.calibration_engine.stpasa_feature_values
        rows: list[dict] = []
        for run_at in self.past_runs(now, days):
            for k in range(0, 336, step):
                start = run_at + k * HALF_HOUR
                if start + HALF_HOUR > now - timedelta(minutes=5):
                    break
                h = k * 0.5
                qni = self.ic_point("NSW1-QLD1", start)
                row: dict[str, Any] = {
                    "interval_time": iso(start),
                    "horizon_hours": round(h, 2),
                    "pd7day_forecast": self.forecast_price(start, run_at),
                    "actual_rrp": round(self.true_price(start), 6),
                    "forecast_run_at": iso(run_at),
                    "hour_of_day": start.astimezone(NEM).hour,
                    "day_of_week": start.astimezone(NEM).weekday(),
                    "month": start.astimezone(NEM).month,
                    "gas_forecast_tj": self.gas_tj(start.astimezone(NEM).date()),
                    "qni_mwflow": qni["mwflow"],
                    "qni_violation_degree": qni["violationdegree"],
                    "is_intervention": False,
                    "actual_source": "golden",
                }
                joined = self._stpasa_for_run(run_at, start)
                if joined is not None:
                    stpasa_run_iso, mw = joined
                    values = transform(
                        mw["surpluscapacity"], mw["ss_solar_uigf"], mw["demand50"],
                        mw["demand10"], mw["demand90"],
                    )
                    if values is not None:
                        (
                            row["stpasa_log_surplus"],
                            row["stpasa_log_solar"],
                            row["stpasa_log_demand"],
                            row["stpasa_poe_spread_n"],
                        ) = values
                        row["stpasa_run_at"] = stpasa_run_iso
                rows.append(row)
        rows.sort(key=lambda r: (r["interval_time"], r["forecast_run_at"]))
        return rows

    def forecast_history(self, mods: Any, now: datetime, *, step: int) -> dict[str, list[dict]]:
        """The forecast history store as ingest_forecast writes it.

        The last two days of runs, plus one run fifteen days old that the
        coordinator's ingest prunes (MAX_FORECAST_AGE_DAYS is 14).
        """
        runs = [r for r in self.past_runs(now, 2) if r >= now - timedelta(days=2)]
        old = now.astimezone(NEM) - timedelta(days=15)
        runs.insert(0, datetime(old.year, old.month, old.day, 7, 30, tzinfo=NEM))
        history: dict[str, list[dict]] = {}
        for run_at in runs:
            for k in range(0, 336, step * 3):
                start = run_at + k * HALF_HOUR
                qni = self.ic_point("NSW1-QLD1", start)
                entry: dict[str, Any] = {
                    "run_at": iso(run_at),
                    "forecast_price": self.forecast_price(start, run_at),
                    "gas_tj": self.gas_tj(start.astimezone(NEM).date()),
                    "qni_mwflow": qni["mwflow"],
                    "qni_violation": qni["violationdegree"],
                    "is_intervention": False,
                    "region": self.region,
                }
                joined = self._stpasa_for_run(run_at, start)
                if joined is not None:
                    stpasa_run_iso, mw = joined
                    entry["stpasa_run_at"] = stpasa_run_iso
                    entry["stpasa_demand10"] = mw["demand10"]
                    entry["stpasa_demand50"] = mw["demand50"]
                    entry["stpasa_demand90"] = mw["demand90"]
                    entry["stpasa_surplus"] = mw["surpluscapacity"]
                    entry["stpasa_solar"] = mw["ss_solar_uigf"]
                    entry["stpasa_wind"] = mw["ss_wind_uigf"]
                history.setdefault(iso(start), []).append(entry)
        return dict(sorted(history.items()))


# ── Builders of the live inputs ───────────────────────────────────────────────

def layout(run_at: datetime, *, long: bool) -> list[datetime]:
    """Interval STARTs of a thinned run: see the module docstring."""
    tiers = [(8.0, 0.5), (36.0, 2.0)]
    if long:
        tiers.append((168.0, 6.0))
    starts: list[datetime] = []
    h = 0.0
    for until, step in tiers:
        while h < until:
            starts.append(run_at + timedelta(hours=h))
            h += step
    return starts


def pd7day_result(
    mods: Any,
    market: SyntheticMarket,
    run_at: datetime,
    *,
    long: bool,
    updated_at: datetime,
) -> Any:
    client = mods.pd7day_client
    const = mods.const
    starts = layout(run_at, long=long)
    source = f"PUBLIC_PD7DAY_{run_at.astimezone(NEM):%Y%m%d%H%M%S}_{market.seed:016d}.zip"
    forecast = [
        client.PricePeriod(
            nemtime=iso(s + HALF_HOUR), time=iso(s), value=market.forecast_price(s, run_at),
        )
        for s in starts
    ]
    min_24h, max_24h = client._min_max_24h(forecast)
    price = client.PD7DayData(
        region=market.region,
        source_file=source,
        forecast_generated_at=iso(run_at),
        interval_minutes=30,
        current_value=forecast[0].value,
        next_value=forecast[1].value if len(forecast) > 1 else None,
        min_24h_value=min_24h,
        max_24h_value=max_24h,
        cheapest_2h_window=client._find_cheapest_window(forecast),
        forecast=forecast,
    )
    interconnectors = {}
    for ic_id in sorted(const.REGION_INTERCONNECTORS[market.region]):
        interconnectors[ic_id] = client.InterconnectorData(
            interconnector_id=ic_id,
            source_file=source,
            run_datetime=iso(run_at),
            forecast=[
                client.InterconnectorPeriod(nemtime=iso(s + HALF_HOUR), time=iso(s), **market.ic_point(ic_id, s))
                for s in starts
            ],
        )
    first_day = run_at.astimezone(NEM).date()
    gas = [
        client.GasForecastPeriod(
            nemtime=f"{(first_day + timedelta(days=i)).isoformat()}T00:00:00+10:00",
            time=iso(datetime.combine(first_day + timedelta(days=i), datetime.min.time(), NEM) - HALF_HOUR),
            value_tj=market.gas_tj(first_day + timedelta(days=i)),
        )
        for i in range(8)
    ]
    return client.PD7DayResult(
        source_file=source,
        case=client.CaseSolutionData(
            run_datetime=iso(run_at), intervention=False, last_changed=iso(run_at - timedelta(days=3)),
        ),
        prices={market.region: price},
        market_summary=client.MarketSummaryData(run_datetime=iso(run_at), forecast=gas),
        interconnectors=interconnectors,
        updated_at=iso(updated_at),
    )


def stpasa_result(mods: Any, market: SyntheticMarket, stpasa_run: datetime, fetched_at: datetime) -> Any:
    sc = mods.stpasa_client
    cov = market.stpasa_coverage_start(stpasa_run)
    intervals = []
    for i in range(6 * 48):
        start = cov + i * HALF_HOUR
        mw = market.stpasa_mw(start)
        window = market.stpasa_surplus_override
        if window is not None and window[0] <= start < window[1]:
            mw = dict(mw, surpluscapacity=round(mw["surpluscapacity"] * window[2], 3))
        intervals.append(sc.StpasaInterval(interval_datetime=iso(start + HALF_HOUR), run_datetime=iso(stpasa_run), **mw))
    return sc.StpasaResult(
        region=market.region,
        run_datetime=iso(stpasa_run),
        intervals=intervals,
        fetched_at=fetched_at.astimezone(UTC).isoformat(),
    )


def dispatch_prices(mods: Any, settlement: datetime, rrp: Mapping[str, float]) -> dict[str, Any]:
    dp = mods.dispatch_client.DispatchPrice
    stamp = settlement.astimezone(NEM).strftime("%Y-%m-%dT%H:%M:%S")
    return {region: dp(region=region, interval_datetime=stamp, rrp=value) for region, value in rrp.items()}


def _lor_notice(notice_id: int, issued: str, level: int, region_word: str, day: str, periods: Sequence[tuple[str, str, str, str]], req: int, avail: int) -> str:
    lines = "\n".join(
        f"[{i}.] From {a} hrs {b} to {c} hrs {d}." for i, (a, b, c, d) in enumerate(periods, 1)
    )
    return (
        f"MARKET NOTICE\nAEMO ELECTRICITY MARKET NOTICE {notice_id} RESERVE NOTICE {issued}\n\n"
        f"STPASA - Forecast Lack Of Reserve Level {level} (LOR{level}) in the {region_word} Region on {day}\n\n"
        f"AEMO declares a Forecast LOR{level} condition for the {region_word} region for the following period:\n"
        f"{lines}\nThe forecast capacity reserve requirement is {req} MW.\n"
        f"The minimum capacity reserve available is {avail} MW.\n\nAEMO Operations\nEND OF REPORT\n"
    )


# ── The twelve scenarios ──────────────────────────────────────────────────────

def _entry_id(name: str) -> str:
    return f"golden_{name}"


def _common(
    mods: Any,
    name: str,
    market: SyntheticMarket,
    *,
    now: datetime,
    run_at: datetime,
    long: bool,
    calibration: CalibrationSeed,
    options: Mapping[str, Any],
    stpasa_age: timedelta | None = timedelta(minutes=25),
    dispatch: Mapping[str, Any] | None = None,
    notices: Sequence[tuple[int, str]] = (),
    usage_fee: float | None = None,
    stale: StaleState | None = None,
    scarcity_samples: Mapping[str, float] | None = None,
) -> Scenario:
    fetch_at = stale.first_fetch_at if stale else now
    stpasa = None
    fetched = None
    if stpasa_age is not None:
        fetched = now - stpasa_age
        local = fetched.astimezone(NEM)
        stpasa_run = datetime(local.year, local.month, local.day, local.hour - local.hour % 2, tzinfo=NEM) - timedelta(hours=2)
        stpasa = stpasa_result(mods, market, stpasa_run, fetched)
    return Scenario(
        name=name,
        region=market.region,
        now=now,
        options=dict(options),
        pd7day=pd7day_result(mods, market, run_at, long=long, updated_at=fetch_at),
        stpasa=stpasa,
        stpasa_fetched_at=fetched,
        calibration=calibration,
        market=market,
        dispatch=dispatch,
        notices=tuple(notices),
        usage_fee=usage_fee,
        stale=stale,
        scarcity_samples=scarcity_samples,
        entry_id=_entry_id(name),
    )


def qld_fitted_evening_peak(mods: Any) -> Scenario:
    """QLD1, stage 1 and stage 2 fitted, days 1-7, Energex tariffs, evening peak."""
    return _common(
        mods, "qld_fitted_evening_peak", SyntheticMarket(seed=11, region="QLD1"),
        now=nem(2026, 9, 15, 18, 40), run_at=nem(2026, 9, 15, 18, 0), long=True,
        calibration=ObservationSeed(),
        options={"forecast_mode": "days_1_7"},
        usage_fee=0.0351,
    )


def qld_days27_mode(mods: Any) -> Scenario:
    """The day 2-7 sensors and trims, inside the Amber Express short window."""
    return _common(
        mods, "qld_days27_mode", SyntheticMarket(seed=12, region="QLD1"),
        now=nem(2026, 9, 16, 8, 10), run_at=nem(2026, 9, 16, 7, 30), long=True,
        calibration=ObservationSeed(),
        options={"forecast_mode": "days_2_7", "active_tariff": "energex/6900"},
    )


def qld_empty_store(mods: Any) -> Scenario:
    """No calibration and no STPASA yet: passthrough everywhere. Options predate forecast_mode."""
    return _common(
        mods, "qld_empty_store", SyntheticMarket(seed=13, region="QLD1"),
        now=nem(2026, 9, 17, 13, 20), run_at=nem(2026, 9, 17, 13, 0), long=False,
        calibration=EmptySeed(), options={}, stpasa_age=None,
    )


def _spike_market(*, tight: bool, lead: str) -> SyntheticMarket:
    """A QLD1 evening spike with gas high, the NSW link depressed or not."""
    if lead == "long":
        spikes = {nem(2026, 9, 19, 17, 0): 4.8, nem(2026, 9, 19, 19, 0): 7.25}
    else:
        spikes = {nem(2026, 9, 18, 18, 0): 4.8, nem(2026, 9, 18, 18, 30): 9.5}
    ic: dict[tuple[str, str], dict[str, float]] = {}
    for start in spikes:
        ic[("NSW1-QLD1", iso(start))] = {"mwflow": 610.0, "exportlimit": 150.0 if tight else 1150.0}
        ic[("N-Q-MNSP1", iso(start))] = {"mwflow": 45.0}
    days = {start.date().isoformat() for start in spikes}
    return SyntheticMarket(
        seed=14, region="QLD1",
        price_overrides={iso(s): v for s, v in spikes.items()},
        ic_overrides=ic,
        gas_overrides={d: 186.5 for d in days},
    )


def qld_spike_credible(mods: Any) -> Scenario:
    """Raw above the spike threshold at 28 h, gas high, network tight (#176)."""
    return _common(
        mods, "qld_spike_credible", _spike_market(tight=True, lead="long"),
        now=nem(2026, 9, 18, 17, 5), run_at=nem(2026, 9, 18, 13, 0), long=False,
        calibration=ObservationSeed(), options={"forecast_mode": "days_1_7"},
    )


def qld_spike_uncredible(mods: Any) -> Scenario:
    """The same spike with the network slack."""
    return _common(
        mods, "qld_spike_uncredible", _spike_market(tight=False, lead="long"),
        now=nem(2026, 9, 18, 17, 5), run_at=nem(2026, 9, 18, 13, 0), long=False,
        calibration=ObservationSeed(), options={"forecast_mode": "days_1_7"},
    )


def qld_short_lead_spike(mods: Any) -> Scenario:
    """A credible spike five hours out, under SPIKE_COVARIATE_MIN_HORIZON_H."""
    return _common(
        mods, "qld_short_lead_spike", _spike_market(tight=True, lead="short"),
        now=nem(2026, 9, 18, 17, 5), run_at=nem(2026, 9, 18, 13, 0), long=False,
        calibration=ObservationSeed(), options={"forecast_mode": "days_1_7"},
    )


def qld_negative_midday(mods: Any) -> Scenario:
    """Negative midday prices below the fitted domain, a market-floor interval,
    and a complete morning window so the QLD1 scarcity premium is active (#114)."""
    run_at = nem(2026, 9, 20, 7, 30)
    overrides: dict[str, float] = {}
    for day in (20, 21):
        for i, value in enumerate((-0.041, -0.118, -0.254, -0.31, -0.2, -0.09)):
            overrides[iso(nem(2026, 9, day, 10, 30) + i * HALF_HOUR)] = value
    overrides[iso(nem(2026, 9, 21, 13, 30))] = -1.0
    market = SyntheticMarket(seed=17, region="QLD1", price_overrides=overrides, midday_depth=0.06)
    samples = {}
    for i in range(36):
        stamp = nem(2026, 9, 20, 7, 5) + timedelta(minutes=5 * i)
        samples[stamp.astimezone(UTC).isoformat()] = round(0.048 + 0.0009 * (i % 7), 6)
    return _common(
        mods, "qld_negative_midday", market,
        now=nem(2026, 9, 20, 11, 10), run_at=run_at, long=False,
        calibration=ObservationSeed(), options={"forecast_mode": "days_1_7"},
        scarcity_samples=samples, usage_fee=0.0,
    )


def qld_stage2_out_of_domain(mods: Any) -> Scenario:
    """The evening-peak fit, served a STPASA run whose surplus is far outside
    the training range for a day: stage 2 is declined there (#147, #153)."""
    market = SyntheticMarket(
        seed=11, region="QLD1",
        stpasa_surplus_override=(nem(2026, 9, 17, 4, 0), nem(2026, 9, 18, 4, 0), 9.0),
    )
    return _common(
        mods, "qld_stage2_out_of_domain", market,
        now=nem(2026, 9, 15, 18, 40), run_at=nem(2026, 9, 15, 18, 0), long=True,
        calibration=ObservationSeed(), options={"forecast_mode": "days_1_7"},
    )


NSW_DISPATCH_RRP = 0.11234


def nsw_dispatch_live(mods: Any) -> Scenario:
    """A dispatch price present: native values follow dispatch, not PD7DAY."""
    now = nem(2026, 9, 21, 14, 12)
    return _common(
        mods, "nsw_dispatch_live", SyntheticMarket(seed=21, region="NSW1"),
        now=now, run_at=nem(2026, 9, 21, 13, 0), long=False,
        calibration=ObservationSeed(),
        options={"forecast_mode": "days_2_7"},
        dispatch=dispatch_prices(mods, nem(2026, 9, 21, 14, 10), {
            "NSW1": NSW_DISPATCH_RRP, "QLD1": 0.09871, "VIC1": 0.07702, "SA1": 0.13105, "TAS1": 0.06544,
        }),
        usage_fee=0.0275,
    )


def sa_stale_coordinator(mods: Any) -> Scenario:
    """Morning run served all day after the evening fetch failed with a 403.

    Set up at 07:41 over a coefficient store from an earlier fit (restored,
    then replaced by the startup refit); the STPASA cache is past its fresh
    window by 19:05."""
    now = nem(2026, 9, 22, 19, 5)
    first = nem(2026, 9, 22, 7, 41)
    return _common(
        mods, "sa_stale_coordinator", SyntheticMarket(seed=31, region="SA1"),
        now=now, run_at=nem(2026, 9, 22, 7, 30), long=False,
        calibration=ObservationSeed(prefit=True),
        options={"forecast_mode": "days_2_7"},
        stpasa_age=timedelta(hours=2, minutes=30),
        stale=StaleState(first_fetch_at=first, failure_status=403),
    )


def vic_prcer_extension(mods: Any) -> Scenario:
    """Powercor PRCER import and export priced from tariff_extensions (#170)."""
    return _common(
        mods, "vic_prcer_extension", SyntheticMarket(seed=41, region="VIC1"),
        now=nem(2026, 9, 23, 16, 20), run_at=nem(2026, 9, 23, 13, 0), long=False,
        calibration=EmptySeed(),
        options={"forecast_mode": "days_2_7", "active_tariff": "powercor/PRCER"},
        usage_fee=0.0125,
    )


def qld_lor2_notice(mods: Any) -> Scenario:
    """A current LOR2 on the binary sensor and chart, an LOR1 two days out, and
    an SA MSL1 that QLD1 must not show. The cancellation of 150200 names it by
    id and by date, and the store's date match also cancels 150215, the other
    LOR1 on 25/09: that is today's behaviour, and the snapshot pins it."""
    notices = [
        (150200, _lor_notice(150200, "23/09/2026 16:02:11", 1, "QLD", "25/09/2026", [("0600", "25/09/2026", "0700", "25/09/2026")], 820, 790)),
        (150211, _lor_notice(150211, "24/09/2026 14:52:10", 2, "QLD", "24/09/2026", [("1700", "24/09/2026", "2000", "24/09/2026")], 905, 640)),
        (150215, _lor_notice(150215, "24/09/2026 15:01:40", 1, "QLD", "25/09/2026", [("1730", "25/09/2026", "1930", "25/09/2026"), ("2100", "25/09/2026", "2130", "25/09/2026")], 1180, 1010)),
        (150218, _lor_notice(150218, "24/09/2026 15:04:02", 1, "QLD", "26/09/2026", [("1800", "26/09/2026", "1900", "26/09/2026")], 1150, 1090)),
        (150220, (
            "MARKET NOTICE\nAEMO ELECTRICITY MARKET NOTICE 150220 MINIMUM SYSTEM LOAD 24/09/2026 15:10:00\n\n"
            "Forecast Minimum System Load (MSL1) condition in the SA region on 27/09/2026\n\n"
            "[1.] From 1130 hrs 27/09/2026 to 1400 hrs 27/09/2026. Minimum regional demand is forecast to be 402 MW at 1300 hrs.\n\n"
            "AEMO Operations\nEND OF REPORT\n"
        )),
        (150225, (
            "MARKET NOTICE\nAEMO ELECTRICITY MARKET NOTICE 150225 RESERVE NOTICE 24/09/2026 15:20:05\n\n"
            "Cancellation of the Forecast Lack Of Reserve Level 1 (LOR1) in the QLD Region on 25/09/2026\n\n"
            "The Forecast LOR1 condition in the QLD region advised in AEMO Electricity Market Notice 150200 has been cancelled.\n"
            "Refer to Market Notice 150200\n\nAEMO Operations\nEND OF REPORT\n"
        )),
    ]
    return _common(
        mods, "qld_lor2_notice", SyntheticMarket(seed=51, region="QLD1"),
        now=nem(2026, 9, 24, 15, 35), run_at=nem(2026, 9, 24, 13, 0), long=False,
        calibration=EmptySeed(), options={"forecast_mode": "days_1_7"},
        notices=notices,
    )


SCENARIOS: dict[str, Callable[[Any], Scenario]] = {
    "qld_fitted_evening_peak": qld_fitted_evening_peak,
    "qld_days27_mode": qld_days27_mode,
    "qld_empty_store": qld_empty_store,
    "qld_spike_credible": qld_spike_credible,
    "qld_spike_uncredible": qld_spike_uncredible,
    "qld_short_lead_spike": qld_short_lead_spike,
    "qld_negative_midday": qld_negative_midday,
    "qld_stage2_out_of_domain": qld_stage2_out_of_domain,
    "nsw_dispatch_live": nsw_dispatch_live,
    "sa_stale_coordinator": sa_stale_coordinator,
    "vic_prcer_extension": vic_prcer_extension,
    "qld_lor2_notice": qld_lor2_notice,
}
