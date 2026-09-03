"""
Persistent storage for parsed GridNoticeAnnotations.

Uses HA homeassistant.helpers.storage.Store (same pattern as calibration_store.py).
Stores per-region notice lists + last_seen_notice_id.
Retention: notices whose period_to < now - 7 days are pruned on each write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.helpers.storage import Store

from .market_notice_client import GridNoticeAnnotation
from .nem_time import now_nem

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

NOTICE_STORE_KEY = "nem_pd7day.notices"
NOTICE_STORE_VERSION = 1
NOTICE_STORE_SCHEMA_VERSION = 2  # Increment to invalidate cached data
NOTICE_RETENTION_DAYS = 7


class GridNoticeStore:
    """
    Persists GridNoticeAnnotations to HA .storage.

    Usage:
        store = GridNoticeStore(hass)
        await store.async_load()
        store.add_notices(new_notices)
        await store.async_save()
        annotations = store.get_active_notices(region="QLD1")
    """

    def __init__(self, hass: "HomeAssistant") -> None:
        self._store = Store(hass, NOTICE_STORE_VERSION, NOTICE_STORE_KEY)
        self._notices: dict[str, list[GridNoticeAnnotation]] = {}
        self._last_seen_notice_id: int = 0
        self.last_fetched_at: datetime | None = None

    async def async_load(self) -> None:
        """Load notices from .storage. Call once on integration setup."""
        data = await self._store.async_load()
        if not data:
            return
        # Schema version check (independent of HA store version).
        # Increment NOTICE_STORE_SCHEMA_VERSION to discard stale cached data.
        # v1 data has no schema_version field; v2 fixes LOR level parsing.
        if data.get("schema_version", 1) < NOTICE_STORE_SCHEMA_VERSION:
            _LOGGER.info(
                "Notice store schema v%d < v%d — discarding cache, will re-fetch",
                data.get("schema_version", 1),
                NOTICE_STORE_SCHEMA_VERSION,
            )
            await self._store.async_remove()
            return
        self._last_seen_notice_id = data.get("last_seen_notice_id", 0)
        for region, notice_list in data.get("notices", {}).items():
            self._notices[region] = [
                GridNoticeAnnotation.from_dict(n) for n in notice_list
            ]
        # Set last_fetched_at to the most recent issued_at across all loaded notices
        all_notices = [n for ns in self._notices.values() for n in ns]
        if all_notices:
            self.last_fetched_at = max(n.issued_at for n in all_notices)
        _LOGGER.debug(
            "Loaded %d notices from storage, last_seen_id=%d",
            sum(len(v) for v in self._notices.values()),
            self._last_seen_notice_id,
        )

    async def async_save(self) -> None:
        """Persist notices to .storage."""
        self._prune()
        data = {
            "schema_version": NOTICE_STORE_SCHEMA_VERSION,
            "last_seen_notice_id": self._last_seen_notice_id,
            "notices": {
                region: [n.to_dict() for n in notices]
                for region, notices in self._notices.items()
            },
        }
        await self._store.async_save(data)

    def add_notices(self, notices: list[GridNoticeAnnotation]) -> None:
        """
        Add new notices. Apply cancellations in two ways:
        1. By explicit cancels_notice_id (if present)
        2. By matching (region, level, cancellation_date) against stored notices'
           period_from date — handles AEMO cancellation notices that don't
           reference a specific notice ID.
        """
        self.last_fetched_at = now_nem()
        for notice in notices:
            region = notice.region
            if region not in self._notices:
                self._notices[region] = []

            if notice.is_cancelled:
                # Path 1: cancel by explicit notice ID reference
                if notice.cancels_notice_id:
                    for existing in self._notices.get(region, []):
                        if existing.notice_id == notice.cancels_notice_id:
                            existing.is_cancelled = True
                            _LOGGER.debug(
                                "Marked notice %d as cancelled (by %d via ID ref)",
                                notice.cancels_notice_id, notice.notice_id,
                            )

                # Path 2: cancel by (region, level, date) matching
                if notice.cancellation_date:
                    for existing in self._notices.get(region, []):
                        if (
                            not existing.is_cancelled
                            and existing.notice_type == notice.notice_type
                            and existing.level == notice.level
                            and existing.period_from.date() == notice.cancellation_date
                        ):
                            existing.is_cancelled = True
                            _LOGGER.debug(
                                "Marked notice %d as cancelled (by %d via date match %s)",
                                existing.notice_id, notice.notice_id,
                                notice.cancellation_date,
                            )

            # Deduplicate: replace if same notice_id already stored
            existing_ids = {n.notice_id for n in self._notices[region]}
            if notice.notice_id in existing_ids:
                self._notices[region] = [
                    n if n.notice_id != notice.notice_id else notice
                    for n in self._notices[region]
                ]
            else:
                self._notices[region].append(notice)

            self._last_seen_notice_id = max(
                self._last_seen_notice_id, notice.notice_id
            )

    def get_active_notices(
        self,
        region: str,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> list[GridNoticeAnnotation]:
        """
        Return non-cancelled notices for region, optionally filtered to overlap
        a time window [from_dt, to_dt].
        """
        notices = [
            n for n in self._notices.get(region, [])
            if not n.is_cancelled
        ]
        if from_dt is not None and to_dt is not None:
            notices = [
                n for n in notices
                if n.period_to >= from_dt and n.period_from <= to_dt
            ]
        return sorted(notices, key=lambda n: n.period_from)

    def get_upcoming_stress(self, region: str, horizon_hours: int = 48) -> list[GridNoticeAnnotation]:
        """
        Return active non-cancelled LOR2+/MSL2+ notices within the next horizon_hours.
        Used by the binary sensor.
        """
        now = now_nem()
        cutoff = now + timedelta(hours=horizon_hours)
        return [
            n for n in self.get_active_notices(region, from_dt=now, to_dt=cutoff)
            if n.level >= 2
        ]

    def has_active_stress(self, region: str, horizon_hours: int = 48) -> bool:
        """True if any LOR2+ or MSL2+ notice is active within horizon_hours."""
        return len(self.get_upcoming_stress(region, horizon_hours)) > 0

    @property
    def last_seen_notice_id(self) -> int:
        return self._last_seen_notice_id

    def advance_cursor(self, notice_id: int) -> bool:
        """Move the cursor forward to notice_id, returning True if it moved.

        add_notices only advances the cursor when a notice was actually stored,
        which is wrong as the sole mechanism: LOR and MSL notices are rare, so
        most cycles store nothing and the cursor would never move. The client
        then re-examines every notice published since the last relevant one,
        which grows without bound.

        The cursor is monotonic. It is a high-water mark of what has been
        examined, not of what has been kept.
        """
        if notice_id <= self._last_seen_notice_id:
            return False
        self._last_seen_notice_id = notice_id
        return True

    def _prune(self) -> None:
        """Remove notices older than NOTICE_RETENTION_DAYS past their period_to."""
        cutoff = now_nem() - timedelta(days=NOTICE_RETENTION_DAYS)
        for region in list(self._notices.keys()):
            self._notices[region] = [
                n for n in self._notices[region]
                if n.period_to >= cutoff
            ]
