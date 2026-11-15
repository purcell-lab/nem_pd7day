#!/usr/bin/env python3
"""Backtest pre-dispatch serving against TradingIS from NEMWEB archives (#337 stage 3b).

Rebuilds what a live install records and serves, from NEMWEB's own files, and
compares, by horizon band, the MAE and bias against the TradingIS actual of:

* **raw** pre-dispatch, as served before v3.50.4;
* the **interim**, v3.50.4: PD7DAY's ``h24_48`` stage 1 from 24 h, else raw;
* **3b**, v3.51.0: pre-dispatch's own fit where a band is admitted, else the
  interim.

Inputs, in one directory (``--data``):

* ``pd_*.zip``: weekly PredispatchIS archives (Reports/Archive/PredispatchIS_Reports);
* ``tis_*.zip``: weekly TradingIS archives (Reports/Archive/TradingIS_Reports);
* ``pd7/*.zip``: PD7DAY runs (Reports/Current/PD7Day), for the PD7DAY fit the
  interim serves through.

The parsed rows are cached in ``DIR/pd_backtest_rows.json.gz``; delete it to reparse.

The pre-dispatch observations are built as stage 3a builds them: each run's
periods thinned by ``predispatch_history.run_forecast``, each paired with the
actual a live install records, the first ``TRADING,PRICE`` row of the TradingIS
file named at the period's end. The PD7DAY observations pair each run's
periods with the same actuals.

Protocol, per region: every day of the test span, as a live install refits
daily, both fits are made on what was known by that midnight. PD7DAY's
stage 1 through ``CalibrationEngine.fit``, pre-dispatch's through
``predispatch_fit.fit_pd_calibration`` with that PD7DAY fit as its interim.
Then the day's observations are served by each of the three and scored.
Spike rows (either side at or above 3 $/kWh) and periods with no actual are
left out, as the fits leave them out.

Usage:
    python scripts/pd_backtest.py --data DIR --train-from 2026-10-11 --test-from 2026-10-25 \\
        --test-to 2026-11-08 [--regions QLD1,NSW1] [--out report.json]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import io
import json
import os
import pathlib
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import support  # noqa: E402

support.install_ha_stubs()
(_const, _engine, _pdc, _pdh, _fit, _cal, _pd7, _serving) = support.load_chain(
    "const", "calibration_engine", "predispatch_client", "predispatch_history",
    "predispatch_fit", "predispatch_calibration", "pd7day_client", "serving",
)
NEM = _const.NEM_TZ
REGIONS = ("NSW1", "QLD1", "SA1", "TAS1", "VIC1")
SPIKE = _serving.SPIKE_THRESHOLD
PERIOD = timedelta(minutes=30)


def _inner_csvs(path: pathlib.Path):
    """(name, CSV text) of every run in a weekly archive of zipped runs."""
    with zipfile.ZipFile(path) as outer:
        for name in sorted(outer.namelist()):
            with zipfile.ZipFile(io.BytesIO(outer.read(name))) as inner:
                csv_name = inner.namelist()[0]
                yield name, inner.read(csv_name).decode("utf-8", errors="ignore")


def load_actuals(data: pathlib.Path) -> dict[str, dict[datetime, float]]:
    """{region: {period end: $/kWh}} from the TradingIS files named at :00 and :30, as a live install reads them."""
    actuals: dict[str, dict[datetime, float]] = defaultdict(dict)
    for path in sorted(data.glob("tis_*.zip")):
        for name, text in _inner_csvs(path):
            stamp = name.split("_")[2]          # PUBLIC_TRADINGIS_YYYYMMDDHHMM_...
            if stamp[-2:] not in ("00", "30"):
                continue
            end = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=NEM)
            seen: set[str] = set()
            for line in text.splitlines():
                parts = line.split(",")
                if len(parts) < 9 or parts[0] != "D" or parts[1] != "TRADING" or parts[2] != "PRICE":
                    continue
                region = parts[6].strip('"')
                if region in seen:
                    continue
                try:
                    actuals[region][end] = round(float(parts[8].strip('"')) / 1000.0, 6)
                    seen.add(region)
                except ValueError:
                    continue
    return actuals


def _pd_run(args: tuple[str, str]) -> list[tuple[str, str, str, float, float]]:
    """One run's kept periods per region: (region, run_at, start, horizon h, $/kWh)."""
    name, text = args
    snapshot = _pdc.parse_predispatch(text, name)
    if snapshot is None:
        return []
    rows = []
    for region in REGIONS:
        for period in _pdh.run_forecast(snapshot, region).forecast:
            start = datetime.fromisoformat(period.time)
            rows.append((region, snapshot.run_at.isoformat(), period.time,
                         round((start - snapshot.run_at).total_seconds() / 3600, 2), period.value))
    return rows


def load_predispatch(data: pathlib.Path) -> list[tuple[str, str, str, float, float]]:
    rows: list[tuple[str, str, str, float, float]] = []
    with concurrent.futures.ProcessPoolExecutor() as pool:
        for path in sorted(data.glob("pd_*.zip")):
            for run_rows in pool.map(_pd_run, _inner_csvs(path), chunksize=8):
                rows.extend(run_rows)
    return rows


def load_pd7day(data: pathlib.Path) -> list[tuple[str, str, str, float, float]]:
    """Each PD7DAY run's periods per region: (region, run_at, start, horizon h, $/kWh)."""
    rows = []
    for path in sorted((data / "pd7").glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0])
        _sol, _case, prices, _ms, _ic = _pd7._parse_all_tables(raw, list(REGIONS), set())
        for region, (run_iso, periods) in prices.items():
            if not run_iso:
                continue
            run_at = datetime.fromisoformat(run_iso)
            for p in periods:
                start = datetime.fromisoformat(p.time)
                h = (start - run_at).total_seconds() / 3600
                if h >= 0:
                    rows.append((region, run_iso, p.time, round(h, 2), p.value))
    return rows


def observations(rows, actuals, region: str) -> list[tuple[datetime, Any]]:
    """The region's rows paired with their actual, as engine Observations, with each period's start."""
    out = []
    for reg, run_at, start_iso, h, value in rows:
        if reg != region:
            continue
        start = datetime.fromisoformat(start_iso)
        actual = actuals.get(region, {}).get(start + PERIOD)
        if actual is None:
            continue
        out.append((start, _engine.Observation(
            interval_time=start_iso, horizon_hours=h, pd7day_forecast=value, actual_rrp=actual,
            forecast_run_at=run_at, hour_of_day=start.hour, day_of_week=start.weekday(), month=start.month,
            gas_forecast_tj=None, qni_mwflow=None, qni_violation_degree=None, is_intervention=False,
        )))
    return out


class _Store:
    """A PD7DAY CalibrationResult behind CalibrationStore.apply_to_price, as the interim reads it."""

    def __init__(self, result: Any, region: str) -> None:
        self._result, self._region = result, region

    def apply_to_price(self, raw, horizon_hours, hour_of_day, *, interval_dt=None):
        return self._result.apply(raw, horizon_hours, hour_of_day, interval_dt=interval_dt, region=self._region)


def backtest(region, pd_obs, pd7_obs, train_from, test_from, test_to) -> dict[str, Any]:
    acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    sources: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    fits = []
    day = test_from
    while day < test_to:
        known_pd = [o for s, o in pd_obs if train_from <= s < day and s + PERIOD <= day]
        known_pd7 = [o for s, o in pd7_obs if s + PERIOD <= day]
        pd7_fit = _engine.CalibrationEngine().fit(known_pd7, region, now=day)
        store = _Store(pd7_fit, region)
        fit = _fit.fit_pd_calibration(
            known_pd, region, day, lambda raw, h, start: _cal.interim_kwh(store, raw, h, start))
        fits.append({"day": day.date().isoformat(), "admitted": sorted(fit.admitted),
                     "train_rows": fit.total_observations})
        record = type("Record", (), {"calibration": fit, "fit_generation": 1})()
        models = _cal.ServingModels(pd7day={region: store}, pd={region: record})
        interim_only = _cal.ServingModels(pd7day={region: store})
        for start, o in pd_obs:
            if not day <= start < day + timedelta(days=1):
                continue
            if o.actual_rrp >= SPIKE or o.pd7day_forecast >= SPIKE:
                continue
            band = _fit.pd_horizon_label(o.horizon_hours)
            if band is None:
                continue
            run_at = datetime.fromisoformat(o.forecast_run_at)
            end = start + PERIOD
            raw_mwh = o.pd7day_forecast * 1000.0
            served = {
                "raw": o.pd7day_forecast,
                "interim": _cal.serve(region, end, raw_mwh, run_at, interim_only).rrp_mwh / 1000.0,
            }
            period = _cal.serve(region, end, raw_mwh, run_at, models)
            served["3b"] = period.rrp_mwh / 1000.0
            sources[band][period.calibrated_source] += 1
            for name, value in served.items():
                cell = acc[band][name]
                cell[0] += abs(value - o.actual_rrp)
                cell[1] += value - o.actual_rrp
                cell[2] += 1
        day += timedelta(days=1)
    table = {}
    for band in _fit.PD_HORIZON_LABELS:
        if band not in acc:
            continue
        row: dict[str, Any] = {"n": acc[band]["raw"][2], "served_by": dict(sources[band])}
        for name in ("raw", "interim", "3b"):
            total_abs, total, n = acc[band][name]
            row[f"{name}_mae_c"] = round(100 * total_abs / n, 3)
            row[f"{name}_bias_c"] = round(100 * total / n, 3)
        table[band] = row
    return {"region": region, "bands": table, "fits": fits}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=pathlib.Path)
    ap.add_argument("--train-from", required=True)
    ap.add_argument("--test-from", required=True)
    ap.add_argument("--test-to", required=True)
    ap.add_argument("--regions", default=",".join(REGIONS))
    ap.add_argument("--out", type=pathlib.Path)
    args = ap.parse_args(argv)
    day = lambda s: datetime.fromisoformat(s).replace(tzinfo=NEM)  # noqa: E731
    cache = args.data / "pd_backtest_rows.json.gz"
    if cache.exists():
        cached = json.loads(gzip.decompress(cache.read_bytes()))
        actuals = {r: {datetime.fromisoformat(k): v for k, v in by.items()} for r, by in cached["actuals"].items()}
        pd_rows, pd7_rows = [tuple(r) for r in cached["pd"]], [tuple(r) for r in cached["pd7"]]
    else:
        actuals = load_actuals(args.data)
        pd_rows = load_predispatch(args.data)
        pd7_rows = load_pd7day(args.data)
        cache.write_bytes(gzip.compress(json.dumps({
            "actuals": {r: {k.isoformat(): v for k, v in by.items()} for r, by in actuals.items()},
            "pd": pd_rows, "pd7": pd7_rows,
        }).encode()))
    print(f"actuals {sum(map(len, actuals.values()))}, pre-dispatch rows {len(pd_rows)}, PD7DAY rows {len(pd7_rows)}",
          file=sys.stderr)
    report = []
    for region in args.regions.split(","):
        result = backtest(region, observations(pd_rows, actuals, region), observations(pd7_rows, actuals, region),
                          day(args.train_from), day(args.test_from), day(args.test_to))
        report.append(result)
        print(f"\n{region}  (MAE and bias in c/kWh against TradingIS; n = scored periods)")
        print(f"{'band':8} {'n':>6} {'raw MAE':>8} {'interim':>8} {'3b':>8} | {'raw bias':>8} {'interim':>8} {'3b':>8}")
        for band, row in result["bands"].items():
            print(f"{band:8} {row['n']:6d} {row['raw_mae_c']:8.2f} {row['interim_mae_c']:8.2f} {row['3b_mae_c']:8.2f} | "
                  f"{row['raw_bias_c']:8.2f} {row['interim_bias_c']:8.2f} {row['3b_bias_c']:8.2f}")
        print("  admitted by day:", "; ".join(f"{f['day'][5:]} {','.join(f['admitted']) or '-'}" for f in result["fits"]))
    if args.out:
        args.out.write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
