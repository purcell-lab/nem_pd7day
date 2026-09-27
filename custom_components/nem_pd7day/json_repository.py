"""
One JSON document in Home Assistant storage, with its legacy-key migration.

The calibration coefficients and the forecast history each live in one Store
scoped to the region. Both were once kept under an unscoped key, and both
migrate the same way on load: when the scoped key holds nothing, the legacy
key is read, and if it has data that data is saved under the scoped key and
used. The legacy key is left in place.

The class takes the store and a factory for the legacy store rather than
building either, so it imports nothing from Home Assistant; any object with
``async_load`` and ``async_save`` serves (spec 004).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)


class JsonRepository:
    """One Home Assistant Store holding a JSON document, with its legacy-key migration."""

    def __init__(self, store: Any, legacy: Callable[[], Any], migration_message: str) -> None:
        """``legacy`` builds the legacy store, only when it is needed.

        ``migration_message`` is logged at INFO with the lower-cased region
        as its one argument when a migration happens.
        """
        self._store = store
        self._legacy = legacy
        self._migration_message = migration_message

    async def load(self, region: str) -> dict | None:
        """The scoped document, else the legacy one saved under the scoped key.

        The legacy store is read only when the scoped load returned None, and
        its data is migrated only when it is truthy; otherwise whatever the
        scoped load returned comes back, None included.
        """
        data = await self._store.async_load()
        if data is None:
            legacy_data = await self._legacy().async_load()
            if legacy_data:
                _LOGGER.info(self._migration_message, region.lower())
                await self._store.async_save(legacy_data)
                data = legacy_data
        return data

    async def save(self, data: dict) -> None:
        await self._store.async_save(data)
