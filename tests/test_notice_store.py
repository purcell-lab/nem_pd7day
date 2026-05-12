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
    # has_active_stress uses datetime.now() internally — verify via get_upcoming_stress
    # with a patched now so the 48h window covers our test notices
    real_datetime = datetime

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    with patch("custom_components.nem_pd7day.notice_store.datetime", FrozenDatetime):
        assert store.has_active_stress("QLD1", horizon_hours=48)  # MSL2 within 48h


def test_notice_store_last_fetched_at_set_on_add():
    """last_fetched_at is set when add_notices is called."""
    hass = MagicMock()
    store = GridNoticeStore(hass)
    assert store.last_fetched_at is None

    now = datetime(2026, 5, 14, 12, 0, tzinfo=NEM_TZ)
    notices = [
        GridNoticeAnnotation(
            notice_id=2001, notice_type="LOR", level=1, region="NSW1",
            period_from=now + timedelta(hours=1),
            period_to=now + timedelta(hours=3),
            issued_at=now, is_cancelled=False,
        ),
    ]
    store.add_notices(notices)
    assert store.last_fetched_at is not None
