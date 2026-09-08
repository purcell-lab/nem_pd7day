"""Tests for GridNoticeStore active-notice filtering and last_fetched_at."""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from custom_components.nem_pd7day.notice_store import GridNoticeStore
from custom_components.nem_pd7day.market_notice_client import GridNoticeAnnotation

NEM_TZ = timezone(timedelta(hours=10))


def test_notice_store_active_count():
    """get_active_notices returns non-cancelled notices within window."""
    hass = MagicMock()
    store = GridNoticeStore(hass)

    now = datetime(2026, 5, 14, 12, 0, tzinfo=NEM_TZ)
    notices = [
        GridNoticeAnnotation(
            notice_id=1001, notice_type="LOR", level=1, region="QLD1",
            period_from=now + timedelta(hours=2),
            period_to=now + timedelta(hours=4),
            issued_at=now, is_cancelled=False,
        ),
        GridNoticeAnnotation(
            notice_id=1002, notice_type="MSL", level=2, region="QLD1",
            period_from=now + timedelta(hours=10),
            period_to=now + timedelta(hours=12),
            issued_at=now, is_cancelled=False,
        ),
        GridNoticeAnnotation(
            notice_id=1003, notice_type="LOR", level=2, region="QLD1",
            period_from=now + timedelta(hours=1),
            period_to=now + timedelta(hours=3),
            issued_at=now, is_cancelled=True,  # cancelled -- should be excluded
        ),
    ]
    store.add_notices(notices)

    active = store.get_active_notices(
        "QLD1",
        from_dt=now,
        to_dt=now + timedelta(days=7),
    )
    assert len(active) == 2
    assert all(not n.is_cancelled for n in active)
    # has_active_stress reads the clock through now_nem() (issue #108); pin it
    # so the 48h window covers our test notices.
    with patch("custom_components.nem_pd7day.notice_store.now_nem", return_value=now):
        assert store.has_active_stress("QLD1", horizon_hours=48)  # MSL2 within 48h


def _one_notice(now, notice_id=2001, issued_at=None):
    return GridNoticeAnnotation(
        notice_id=notice_id, notice_type="LOR", level=1, region="NSW1",
        period_from=now + timedelta(hours=1),
        period_to=now + timedelta(hours=3),
        issued_at=issued_at or now, is_cancelled=False,
    )


def test_last_fetched_at_is_stamped_by_mark_fetched_not_by_add_notices():
    """last_fetched_at means "NEMWEB was polled", not "a notice was stored".

    Issue #139: it was set only in add_notices, so with no relevant notice in
    the market it sat days old across every scheduled refresh and a broken
    poll looked identical to a quiet grid.
    """
    hass = MagicMock()
    store = GridNoticeStore(hass)
    assert store.last_fetched_at is None

    now = datetime(2026, 5, 14, 12, 0, tzinfo=NEM_TZ)
    store.add_notices([_one_notice(now)])
    assert store.last_fetched_at is None, "storing a notice is not a poll"

    polled = datetime(2026, 5, 16, 13, 0, tzinfo=NEM_TZ)
    with patch("custom_components.nem_pd7day.notice_store.now_nem", return_value=polled):
        store.mark_fetched()
    assert store.last_fetched_at == polled


def test_last_notice_issued_at_is_the_newest_stored_notice():
    """The old meaning of last_fetched survives under its own name."""
    hass = MagicMock()
    store = GridNoticeStore(hass)
    assert store.last_notice_issued_at is None

    now = datetime(2026, 5, 14, 12, 0, tzinfo=NEM_TZ)
    older = now - timedelta(days=1)
    store.add_notices([
        _one_notice(now, notice_id=2001, issued_at=older),
        _one_notice(now, notice_id=2002, issued_at=now),
    ])
    assert store.last_notice_issued_at == now
    # Unaffected by polling.
    store.mark_fetched()
    assert store.last_notice_issued_at == now


def test_async_load_does_not_derive_last_fetched_at_from_notices():
    """On load the poll stamp stays None until the first poll of the session."""
    import asyncio
    from custom_components.nem_pd7day.notice_store import NOTICE_STORE_SCHEMA_VERSION

    hass = MagicMock()
    store = GridNoticeStore(hass)
    now = datetime(2026, 5, 14, 12, 0, tzinfo=NEM_TZ)
    payload = {
        "schema_version": NOTICE_STORE_SCHEMA_VERSION,
        "last_seen_notice_id": 4321,
        "notices": {"NSW1": [_one_notice(now).to_dict()]},
    }

    async def _load():
        return payload

    store._store = MagicMock()
    store._store.async_load = _load
    asyncio.run(store.async_load())

    assert store.last_seen_notice_id == 4321
    assert store.last_notice_issued_at == now
    assert store.last_fetched_at is None


def test_advance_cursor_moves_forward_only():
    """The cursor is a monotonic high-water mark of what has been examined."""
    store = GridNoticeStore.__new__(GridNoticeStore)
    store._notices = {}
    store._last_seen_notice_id = 5000
    store.last_fetched_at = None

    assert store.advance_cursor(5100) is True
    assert store.last_seen_notice_id == 5100

    # A stale or out-of-order value must not rewind it, which would re-open the
    # whole backlog for re-reading.
    assert store.advance_cursor(5050) is False
    assert store.last_seen_notice_id == 5100
    assert store.advance_cursor(5100) is False
    assert store.last_seen_notice_id == 5100


def test_advance_cursor_from_cold_start():
    store = GridNoticeStore.__new__(GridNoticeStore)
    store._notices = {}
    store._last_seen_notice_id = 0
    store.last_fetched_at = None

    assert store.advance_cursor(1) is True
    assert store.last_seen_notice_id == 1
