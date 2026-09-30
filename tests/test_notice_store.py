"""Tests for notice_store.GridNoticeStore.

History kept here:

* Cancellations. AEMO cancels either by naming the notice ("Refer to Market
  Notice N") or, in PDPASA cancellations, by (region, level, date) only, so
  add_notices has to match on both. These used to be checked against an
  inline re-implementation of the store; they now run against the real one.
* last_fetched_at (issue #139) was stamped only in add_notices, so with no
  relevant notice in the market it sat days old across every scheduled
  refresh and a broken poll looked identical to a quiet grid.
* has_active_stress reads the clock through now_nem() (issue #108), so a test
  pins it there.
* The cursor is a monotonic high-water mark of what has been examined, not
  of what has been kept; rewinding it re-opens the whole backlog.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()
_const, _nem_time, _retry, _mnc, _ns = load_chain(
    "const", "nem_time", "nemweb_retry", "market_notice_client", "notice_store"
)

GridNoticeStore = _ns.GridNoticeStore
GridNoticeAnnotation = _mnc.GridNoticeAnnotation

NOW = datetime(2026, 5, 14, 12, 0, tzinfo=NEM_TZ)


def _notice(
    notice_id: int,
    *,
    region="NSW1",
    notice_type="LOR",
    level=1,
    period_from=None,
    period_to=None,
    issued_at=None,
    **fields,
) -> GridNoticeAnnotation:
    return GridNoticeAnnotation(
        notice_id=notice_id,
        notice_type=notice_type,
        level=level,
        region=region,
        period_from=period_from or NOW + timedelta(hours=1),
        period_to=period_to or NOW + timedelta(hours=3),
        issued_at=issued_at or NOW,
        **fields,
    )


def _cancellation(notice_id: int, *, region, notice_type="LOR", level=1, **fields):
    """A cancellation notice: the parser leaves its period at issued_at."""
    return _notice(
        notice_id, region=region, notice_type=notice_type, level=level,
        period_from=NOW, period_to=NOW, is_cancelled=True, **fields,
    )


def test_get_active_notices_excludes_cancelled_and_feeds_has_active_stress():
    store = GridNoticeStore(MagicMock())
    store.add_notices([
        _notice(1001, region="QLD1", level=1,
                period_from=NOW + timedelta(hours=2), period_to=NOW + timedelta(hours=4)),
        _notice(1002, region="QLD1", notice_type="MSL", level=2,
                period_from=NOW + timedelta(hours=10), period_to=NOW + timedelta(hours=12)),
        _notice(1003, region="QLD1", level=2,
                period_from=NOW + timedelta(hours=1), period_to=NOW + timedelta(hours=3),
                is_cancelled=True),
    ])

    active = store.get_active_notices("QLD1", from_dt=NOW, to_dt=NOW + timedelta(days=7))
    assert [n.notice_id for n in active] == [1001, 1002]
    assert all(not n.is_cancelled for n in active)

    # Pinned so the 48 h window covers the MSL2 above (issue #108).
    with patch.object(_ns, "now_nem", return_value=NOW):
        assert store.has_active_stress("QLD1", horizon_hours=48)


def test_add_notices_cancels_by_notice_id_reference():
    store = GridNoticeStore(MagicMock())
    original = _notice(124467, region="VIC1", notice_type="MSL")
    store.add_notices([original])
    assert store.get_active_notices("VIC1") == [original]

    store.add_notices([
        _cancellation(124560, region="VIC1", notice_type="MSL", cancels_notice_id=124467),
    ])
    assert original.is_cancelled
    assert store.get_active_notices("VIC1") == []


def test_add_notices_cancels_by_region_level_and_date():
    """A cancellation naming no notice ID cancels every stored notice of the
    same type and level whose period starts on the cancelled date, in its own
    region only."""
    day = date(2026, 5, 18)
    start = datetime(2026, 5, 18, 6, 0, tzinfo=NEM_TZ)
    end = datetime(2026, 5, 18, 19, 30, tzinfo=NEM_TZ)
    store = GridNoticeStore(MagicMock())
    qld_lor1_a = _notice(144108, region="QLD1", level=1, period_from=start, period_to=end)
    qld_lor1_b = _notice(144109, region="QLD1", level=1, period_from=start,
                         period_to=datetime(2026, 5, 18, 22, 0, tzinfo=NEM_TZ))
    qld_lor2 = _notice(144111, region="QLD1", level=2, period_from=start, period_to=end)
    nsw_lor1 = _notice(144110, region="NSW1", level=1, period_from=start, period_to=end)
    store.add_notices([qld_lor1_a, qld_lor1_b, qld_lor2, nsw_lor1])
    assert len(store.get_active_notices("QLD1")) == 3

    store.add_notices([_cancellation(144114, region="QLD1", level=1, cancellation_date=day)])

    assert qld_lor1_a.is_cancelled and qld_lor1_b.is_cancelled
    assert store.get_active_notices("QLD1") == [qld_lor2]
    assert store.get_active_notices("NSW1") == [nsw_lor1]


def test_a_cancellation_naming_a_notice_cancels_only_that_notice():
    """AEMO's referenced format names one notice and also carries a date, the
    time the cancellation takes effect. The date match must not run on it, or
    every other notice of the same type and level that day is cancelled too
    (#182: cancelling 150200 also cancelled 150215 in the golden master)."""
    start = datetime(2026, 5, 18, 16, 0, tzinfo=NEM_TZ)
    store = GridNoticeStore(MagicMock())
    named = _notice(150200, region="QLD1", level=1, period_from=start,
                    period_to=datetime(2026, 5, 18, 19, 0, tzinfo=NEM_TZ))
    same_day = _notice(150215, region="QLD1", level=1,
                       period_from=datetime(2026, 5, 18, 17, 30, tzinfo=NEM_TZ),
                       period_to=datetime(2026, 5, 18, 20, 30, tzinfo=NEM_TZ))
    store.add_notices([named, same_day])

    store.add_notices([_cancellation(
        150230, region="QLD1", level=1, cancels_notice_id=150200, cancellation_date=date(2026, 5, 18),
    )])

    assert named.is_cancelled
    assert not same_day.is_cancelled
    assert store.get_active_notices("QLD1") == [same_day]


def test_last_fetched_at_is_stamped_by_mark_fetched_not_by_add_notices():
    """last_fetched_at means "NEMWEB was polled", not "a notice was stored"
    (issue #139)."""
    store = GridNoticeStore(MagicMock())
    assert store.last_fetched_at is None

    store.add_notices([_notice(2001)])
    assert store.last_fetched_at is None, "storing a notice is not a poll"

    polled = datetime(2026, 5, 16, 13, 0, tzinfo=NEM_TZ)
    with patch.object(_ns, "now_nem", return_value=polled):
        store.mark_fetched()
    assert store.last_fetched_at == polled


def test_last_notice_issued_at_is_the_newest_stored_notice():
    """The old meaning of last_fetched survives under its own name."""
    store = GridNoticeStore(MagicMock())
    assert store.last_notice_issued_at is None

    store.add_notices([
        _notice(2001, issued_at=NOW - timedelta(days=1)),
        _notice(2002, issued_at=NOW),
    ])
    assert store.last_notice_issued_at == NOW
    # Unaffected by polling.
    store.mark_fetched()
    assert store.last_notice_issued_at == NOW


def test_async_load_does_not_derive_last_fetched_at_from_notices():
    """On load the poll stamp stays None until the first poll of the session."""
    store = GridNoticeStore(MagicMock())
    payload = {
        "schema_version": _ns.NOTICE_STORE_SCHEMA_VERSION,
        "last_seen_notice_id": 4321,
        "notices": {"NSW1": [_notice(2001).to_dict()]},
    }

    async def _load():
        return payload

    store._store = MagicMock()
    store._store.async_load = _load
    run_async(store.async_load())

    assert store.last_seen_notice_id == 4321
    assert store.last_notice_issued_at == NOW
    assert store.last_fetched_at is None


def test_advance_cursor_is_monotonic():
    """The cursor moves forward from a cold start and never rewinds."""
    store = GridNoticeStore(MagicMock())
    assert store.last_seen_notice_id == 0

    assert store.advance_cursor(1) is True
    assert store.last_seen_notice_id == 1
    assert store.advance_cursor(5100) is True
    assert store.last_seen_notice_id == 5100

    # A stale or out-of-order value must not rewind it, which would re-open
    # the whole backlog for re-reading.
    assert store.advance_cursor(5050) is False
    assert store.last_seen_notice_id == 5100
    assert store.advance_cursor(5100) is False
    assert store.last_seen_notice_id == 5100


# ── #216: the real SA1 sequence of 29 and 30 September 2026 ─────────────────


def _parsed(notice_id: int) -> GridNoticeAnnotation:
    from notice_fixtures import notice_text

    return _mnc._parse_notice_body(notice_text(notice_id), notice_id)


SA1_SEQUENCE = (145393, 145394, 145396, 145397, 145409, 145415, 145432)


def test_real_sa1_sequence_leaves_only_the_two_current_msl_notices():
    """What sensor.nem_pd7day_sa1_grid_notices should have shown at 20:39 on
    30 September: 145409 (the update to 145393) and 145432. Before #216 it
    showed 145396 and 145393 as well: a resolved LOR3 and a replaced MSL1."""
    store = GridNoticeStore(MagicMock())
    store.add_notices([_parsed(n) for n in SA1_SEQUENCE])
    now = datetime(2026, 9, 30, 20, 39, tzinfo=NEM_TZ)
    active = store.get_active_notices("SA1", from_dt=now, to_dt=now + timedelta(days=7))
    assert [n.notice_id for n in active] == [145409, 145432]
    assert {n.notice_type for n in active} == {"MSL"}


def test_each_sa1_withdrawal_names_what_it_withdrew():
    store = GridNoticeStore(MagicMock())
    store.add_notices([_parsed(n) for n in SA1_SEQUENCE])
    by_id = {n.notice_id: n for n in store._notices["SA1"]}
    assert by_id[145396].is_cancelled is True          # resolved by 145397
    assert by_id[145394].is_cancelled is True          # cancelled by 145415
    assert by_id[145393].is_cancelled is False         # replaced, not cancelled
    assert by_id[145393].superseded_by == 145409
    assert by_id[145432].is_cancelled is False
    assert by_id[145432].superseded_by is None


def test_an_update_to_a_notice_the_store_never_saw_retires_nothing():
    store = GridNoticeStore(MagicMock())
    other = _notice(145400, region="SA1", notice_type="MSL")
    update = _notice(145409, region="SA1", notice_type="MSL", supersedes_notice_id=145393)
    store.add_notices([other, update])
    assert [n.notice_id for n in store.get_active_notices("SA1")] == [145400, 145409]


def test_an_update_retires_only_the_notice_it_names():
    """Same region, type, level and day: only the named notice goes. The
    date-wide match #182 removed must not come back through updates."""
    store = GridNoticeStore(MagicMock())
    named = _notice(145393, region="SA1", notice_type="MSL")
    sibling = _notice(145394, region="SA1", notice_type="MSL")
    update = _notice(145409, region="SA1", notice_type="MSL", supersedes_notice_id=145393)
    store.add_notices([named, sibling, update])
    assert [n.notice_id for n in store.get_active_notices("SA1")] == [145394, 145409]
