"""Guards that PD7DAY is downloaded and parsed once per cycle, not once per region.

The PD7DAY archive holds every NEM region and every interconnector, yet each
region coordinator used to fetch and parse its own copy. On a five-region install
that was five downloads of ~4.6 MB and five parses of the same ~45 MB CSV per
cycle, about 3,154 ms of CPU where one all-region parse costs about 700 ms.

These tests assert the property that matters: the number of downloads and parses
must not scale with the number of configured regions, and each coordinator must
still receive exactly the slice of data it received before, so single-region
calibration stores cannot be cross-contaminated.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
from datetime import timedelta, timezone
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEM_TZ = timezone(timedelta(hours=10))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.modules.setdefault("aiohttp", MagicMock())

_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_load(
    "custom_components.nem_pd7day.executor",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "executor.py"),
)
_load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_client_mod = _load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)
_shared_mod = _load(
    "custom_components.nem_pd7day.pd7day_shared",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_shared.py"),
)

from custom_components.nem_pd7day.const import (  # noqa: E402
    REGION_INTERCONNECTORS,
    REGIONS,
)
from custom_components.nem_pd7day.pd7day_client import PD7DayClient  # noqa: E402
from custom_components.nem_pd7day.nemweb_retry import (  # noqa: E402
    NemwebFetchError,
)
from custom_components.nem_pd7day.pd7day_shared import (  # noqa: E402
    ALL_INTERCONNECTORS,
    SharedPD7DayFetch,
    result_for_regions,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_zip(csv_bytes: bytes, member: str = "PUBLIC_PD7DAY_X.CSV") -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, csv_bytes)
    return buf.getvalue()


def _all_region_csv() -> bytes:
    """A PD7DAY CSV carrying every region and two interconnectors.

    Column layout mirrors the real files, as asserted in test_pd7day_client.py.
    Timestamps are relative so the fixture never ages out.
    """
    run = _nem_time.now_nem().replace(minute=0, second=0, microsecond=0)
    run_s = run.strftime("%Y/%m/%d %H:%M:%S")
    period_s = (run + timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S")
    price_tail = ",0,0,0,0,0,0,0,0,0,0,0"

    rows = [
        "C,NEOD,PD7DAY,1,PUBLIC_PD7DAY_X.zip",
        f"D,PD7DAY,CASESOLUTION,1,{run_s},0,{run_s}",
    ]
    # One distinct price per region so filtering errors are visible.
    for i, region in enumerate(REGIONS):
        rrp = 10000.00 + i * 1000
        rows.append(
            f"D,PD7DAY,PRICESOLUTION,1,{run_s},1,{period_s},{region},"
            f"{rrp:.2f}{price_tail}"
        )
    # Interconnectors belonging to different regions: NSW1-QLD1 is in QLD1's and
    # NSW1's sets, T-V-MNSP1 is in TAS1's and VIC1's.
    for ic in ("NSW1-QLD1", "T-V-MNSP1"):
        rows.append(
            f"D,PD7DAY,INTERCONNECTORSOLUTION,1,{run_s},1,{period_s},{ic},"
            "0,100,1,0,0,500,500,1.0"
        )
    # Bulk rows the parser reads and discards, as in the real archive where
    # CONSTRAINTSOLUTION is 98.8% of all lines.
    for n in range(50):
        rows.append(f"D,PD7DAY,CONSTRAINTSOLUTION,1,{run_s},1,{period_s},C{n},0,0")
    return "\n".join(rows).encode("utf-8")


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        # The NEMWEB clients inspect resp.status via classify_status now
        # instead of calling raise_for_status, so the stub must carry a
        # status and headers. raise_for_status is kept as a no-op so any
        # remaining caller still works.
        self.status = status
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def read(self):
        return self._payload

    async def text(self, *args, **kwargs):
        return self._payload.decode("utf-8", "replace")


class _CountingSession:
    """Serves a directory listing then the ZIP, counting each kind of request.

    ``listing_name`` is mutable so a test can simulate AEMO publishing a new file.
    """

    def __init__(self, payload: bytes, listing_name: str = "PUBLIC_PD7DAY_1.zip"):
        self.payload = payload
        self.listing_name = listing_name
        self.listing_requests = 0
        self.file_requests = 0
        self.fail_next_listing: Exception | None = None

    def get(self, url, **kwargs):
        if url.endswith("/"):
            self.listing_requests += 1
            if self.fail_next_listing is not None:
                exc, self.fail_next_listing = self.fail_next_listing, None
                raise exc
            href = f"/Reports/Current/PD7Day/{self.listing_name}"
            return _FakeResp(f'<a href="{href}">z</a>'.encode())
        self.file_requests += 1
        return _FakeResp(self.payload)


class _FakeClock:
    """Injectable monotonic clock so burst-window behaviour is deterministic."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _ParseSpy:
    """Counts real _parse_all_tables invocations and the regions requested."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._real = _client_mod._parse_all_tables

    def __enter__(self):
        def spy(csv_bytes, regions, ic_ids):
            self.calls.append(list(regions))
            return self._real(csv_bytes, regions, ic_ids)

        _client_mod._parse_all_tables = spy
        return self

    def __exit__(self, *exc):
        _client_mod._parse_all_tables = self._real
        return False

    @property
    def count(self) -> int:
        return len(self.calls)


def _make_fetcher(clock=None, session=None):
    session = session or _CountingSession(_make_zip(_all_region_csv()))
    fetcher = SharedPD7DayFetch(
        PD7DayClient(session, interconnector_ids=ALL_INTERCONNECTORS),
        clock=clock or _FakeClock(),
    )
    return fetcher, session


# ── The core property: work does not scale with region count ─────────────────


def test_five_concurrent_regions_cause_one_download_and_one_parse():
    """The startup fan-out: all five coordinators refresh at once."""
    fetcher, session = _make_fetcher()

    async def scenario():
        with _ParseSpy() as spy:
            results = await asyncio.gather(
                *(fetcher.fetch_all([r], REGION_INTERCONNECTORS[r]) for r in REGIONS)
            )
            return results, spy.count

    results, parses = asyncio.run(scenario())

    assert len(results) == 5
    assert parses == 1, f"CSV was parsed {parses} times for 5 regions, expected 1"
    assert session.file_requests == 1, (
        f"archive was downloaded {session.file_requests} times, expected 1"
    )
    assert fetcher.stats.downloads == 1
    assert fetcher.stats.burst_hits == 4
    # Every caller still got its own region's data.
    for region, result in zip(REGIONS, results):
        assert region in result.prices


@pytest.mark.parametrize("region_count", [1, 2, 3, 4, 5])
def test_download_count_does_not_scale_with_region_count(region_count):
    """One download and one parse regardless of how many regions are configured."""
    fetcher, session = _make_fetcher()
    regions = REGIONS[:region_count]

    async def scenario():
        with _ParseSpy() as spy:
            for r in regions:
                await fetcher.fetch_all([r], REGION_INTERCONNECTORS[r])
            return spy.count

    parses = asyncio.run(scenario())

    assert parses == 1, f"{region_count} regions caused {parses} parses"
    assert session.file_requests == 1, (
        f"{region_count} regions caused {session.file_requests} downloads"
    )


def test_sequential_fetches_inside_the_burst_window_reuse_the_parse():
    """The staggered startup refreshes land 30 s to 50 s apart, inside the window."""
    clock = _FakeClock()
    fetcher, session = _make_fetcher(clock=clock)

    async def scenario():
        with _ParseSpy() as spy:
            for i, region in enumerate(REGIONS):
                await fetcher.fetch_all([region], REGION_INTERCONNECTORS[region])
                clock.advance(5)  # matches the 5 s stagger between coordinators
            return spy.count

    parses = asyncio.run(scenario())

    assert parses == 1
    assert session.listing_requests == 1, (
        "inside the burst window no directory listing should be needed"
    )


# ── Behaviour once the burst window has lapsed ───────────────────────────────


def test_unchanged_newest_file_reuses_the_parse_without_downloading():
    """AEMO publishes ~3 times a day, so a later caller usually sees the same file."""
    clock = _FakeClock()
    fetcher, session = _make_fetcher(clock=clock)

    async def scenario():
        with _ParseSpy() as spy:
            await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])
            clock.advance(3600)  # an hour later, well past the window
            await fetcher.fetch_all(["NSW1"], REGION_INTERCONNECTORS["NSW1"])
            return spy.count

    parses = asyncio.run(scenario())

    assert parses == 1, "a file already parsed was parsed again"
    assert session.file_requests == 1, "a file already downloaded was downloaded again"
    assert session.listing_requests == 2, "the newest file should be re-checked"
    assert fetcher.stats.same_file_hits == 1


def test_new_publication_triggers_a_fresh_download():
    """Correctness does not depend on the burst window: a new file is picked up."""
    clock = _FakeClock()
    fetcher, session = _make_fetcher(clock=clock)

    async def scenario():
        with _ParseSpy() as spy:
            first = await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])
            clock.advance(3600)
            session.listing_name = "PUBLIC_PD7DAY_2.zip"  # AEMO published
            second = await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])
            return spy.count, first, second

    parses, first, second = asyncio.run(scenario())

    assert parses == 2, "a newly published file was not parsed"
    assert fetcher.stats.downloads == 2
    assert first.source_file != second.source_file


def test_reused_parse_is_restamped_so_the_disk_cache_stays_usable():
    """ForecastStore drops a cache older than 35 minutes.

    updated_at records when the data was last confirmed to be the newest
    published file. Reusing a parse without restamping would make a genuinely
    current forecast look stale and force a blocking fetch on the next restart.
    """
    clock = _FakeClock()
    fetcher, _ = _make_fetcher(clock=clock)

    # Other test modules reload nem_time under the same name, so patch whichever
    # module object is live in sys.modules rather than the one imported here.
    nem_time = sys.modules["custom_components.nem_pd7day.nem_time"]
    base = nem_time.now_nem().replace(microsecond=0)
    times = [base, base + timedelta(minutes=40)]
    real_now = nem_time.now_nem

    async def scenario():
        nem_time.now_nem = lambda: times[0]
        try:
            first = await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])
            clock.advance(3600)
            nem_time.now_nem = lambda: times[1]
            second = await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])
            return first, second
        finally:
            nem_time.now_nem = real_now

    first, second = asyncio.run(scenario())

    assert second.updated_at != first.updated_at, (
        "reused parse kept its original updated_at and would read as stale"
    )
    assert second.updated_at == nem_time.to_nem_iso(times[1])
    # The forecast content itself is unchanged, only the confirmation time moved.
    assert second.source_file == first.source_file
    assert second.prices["QLD1"].current_value == first.prices["QLD1"].current_value


# ── Failure handling ─────────────────────────────────────────────────────────


def test_a_failed_fetch_is_not_cached():
    """The next cycle has to be able to do real work.

    The client raises NemwebFetchError once its own retry budget is spent, and
    a failure must not be memoised: the following fetch has to reach the
    network rather than replay the stored exception.
    """
    fetcher, session = _make_fetcher()
    session.fail_next_listing = RuntimeError("403 Forbidden")

    async def scenario():
        with pytest.raises(NemwebFetchError):
            await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])
        # The next fetch must reach the network, not a memoised failure.
        return await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])

    result = asyncio.run(scenario())

    assert "QLD1" in result.prices
    assert fetcher.stats.downloads == 1


def test_concurrent_callers_all_observe_a_failure():
    """No caller may silently receive an empty or partial result."""
    fetcher, session = _make_fetcher()
    session.fail_next_listing = RuntimeError("403 Forbidden")

    async def scenario():
        return await asyncio.gather(
            *(
                fetcher.fetch_all([r], REGION_INTERCONNECTORS[r])
                for r in REGIONS
            ),
            return_exceptions=True,
        )

    results = asyncio.run(scenario())

    failures = [r for r in results if isinstance(r, Exception)]
    successes = [r for r in results if not isinstance(r, Exception)]
    assert failures, "the fetch failure was swallowed"
    # Whichever callers did succeed must hold real data, never a hollow result.
    for ok in successes:
        assert ok.prices, "a caller received a result with no prices"


# ── Per-region filtering ─────────────────────────────────────────────────────


def test_each_region_sees_only_its_own_prices():
    """The coordinator ingests every region in prices into its single-region store.

    Handing it the unfiltered all-region result would cross-contaminate
    calibration data between regions.
    """
    fetcher, _ = _make_fetcher()

    async def scenario():
        return {
            region: await fetcher.fetch_all([region], REGION_INTERCONNECTORS[region])
            for region in REGIONS
        }

    per_region = asyncio.run(scenario())

    for region, result in per_region.items():
        assert set(result.prices) == {region}, (
            f"{region} coordinator also received {set(result.prices) - {region}}"
        )


def test_each_region_sees_only_its_own_interconnectors():
    fetcher, _ = _make_fetcher()

    async def scenario():
        return (
            await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"]),
            await fetcher.fetch_all(["TAS1"], REGION_INTERCONNECTORS["TAS1"]),
        )

    qld, tas = asyncio.run(scenario())

    assert set(qld.interconnectors) <= REGION_INTERCONNECTORS["QLD1"]
    assert set(tas.interconnectors) <= REGION_INTERCONNECTORS["TAS1"]
    # The fixture carries NSW1-QLD1 (QLD1's) and T-V-MNSP1 (TAS1's).
    assert "NSW1-QLD1" in qld.interconnectors
    assert "T-V-MNSP1" in tas.interconnectors
    assert "T-V-MNSP1" not in qld.interconnectors


def test_shared_parse_covers_every_region_and_interconnector():
    """The one parse must be wide enough to serve any caller's subset.

    If it were narrowed, a region would silently receive no data rather than
    triggering a second parse.
    """
    fetcher, _ = _make_fetcher()

    async def scenario():
        with _ParseSpy() as spy:
            await fetcher.fetch_all(["TAS1"], REGION_INTERCONNECTORS["TAS1"])
            return spy.calls

    calls = asyncio.run(scenario())

    assert calls, "no parse happened"
    assert set(calls[0]) == set(REGIONS), (
        f"the shared parse only covered {calls[0]}, so other regions would be empty"
    )
    for region, ics in REGION_INTERCONNECTORS.items():
        assert ics <= ALL_INTERCONNECTORS, f"{region} interconnectors not covered"


def test_result_for_regions_leaves_the_source_result_untouched():
    """Filtering must not mutate the shared result other callers are holding."""
    fetcher, _ = _make_fetcher()

    async def scenario():
        await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])
        full = fetcher._full
        before_prices = set(full.prices)
        before_ics = set(full.interconnectors)
        result_for_regions(full, ["TAS1"], REGION_INTERCONNECTORS["TAS1"])
        return before_prices, before_ics, set(full.prices), set(full.interconnectors)

    before_p, before_i, after_p, after_i = asyncio.run(scenario())

    assert before_p == after_p
    assert before_i == after_i


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_coordinator_fetches_through_the_shared_fetcher_when_registered():
    """Proves the wiring, not just the class in isolation.

    Without this, the shared fetcher could exist and be perfectly correct while
    every coordinator quietly kept using its own private client.
    """
    for ha_mod in [
        "homeassistant",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.event",
        "homeassistant.helpers.storage",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.util",
        "homeassistant.util.dt",
    ]:
        sys.modules.setdefault(ha_mod, MagicMock())

    class _FakeCoordinator:
        def __init__(self, hass, logger, name, update_interval):
            self.hass = hass

        def __class_getitem__(cls, item):
            return cls

    uc = MagicMock()
    uc.DataUpdateCoordinator = _FakeCoordinator
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})
    sys.modules["homeassistant.helpers.update_coordinator"] = uc

    coord_mod = _load(
        "custom_components.nem_pd7day.coordinator",
        os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
    )
    from custom_components.nem_pd7day.const import DOMAIN, SHARED_FETCH_KEY

    fetcher, _ = _make_fetcher()

    coord = coord_mod.PD7DayCoordinator.__new__(coord_mod.PD7DayCoordinator)
    coord.hass = MagicMock()
    coord.hass.data = {DOMAIN: {SHARED_FETCH_KEY: fetcher}}
    coord._session = None
    coord._regions = ["QLD1"]
    coord._interconnector_ids = REGION_INTERCONNECTORS["QLD1"]

    assert coord._get_client() is fetcher, (
        "coordinator built its own client while a shared fetcher was registered"
    )

    # And with nothing registered it must still stand alone. Compared by name
    # because reloading coordinator.py rebinds its own PD7DayClient class object.
    coord.hass.data = {DOMAIN: {}}
    assert type(coord._get_client()).__name__ == "PD7DayClient"


def test_shared_fetch_holds_no_reference_to_the_event_loop_thread():
    """Parsing still happens off the loop after centralisation.

    The archive is now parsed for five regions instead of one, so regressing the
    executor hand-off here would be worse than before it was introduced.
    """
    fetcher, _ = _make_fetcher()
    seen: dict[str, int] = {}
    real = _client_mod._parse_all_tables

    def spy(csv_bytes, regions, ic_ids):
        seen["parse"] = threading.get_ident()
        return real(csv_bytes, regions, ic_ids)

    async def scenario():
        seen["loop"] = threading.get_ident()
        _client_mod._parse_all_tables = spy
        try:
            await fetcher.fetch_all(["QLD1"], REGION_INTERCONNECTORS["QLD1"])
        finally:
            _client_mod._parse_all_tables = real

    asyncio.run(scenario())

    assert "parse" in seen
    assert seen["parse"] != seen["loop"], (
        "the shared all-region parse ran on the event loop thread"
    )
