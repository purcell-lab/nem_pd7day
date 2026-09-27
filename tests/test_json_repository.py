"""
JsonRepository: one stored JSON document and its legacy-key migration
(spec 004). CalibrationStore uses one for the coefficient file and one for
the forecast history file; the migration rule is the one both used to spell
out inline.

Run with:  python -m pytest tests/test_json_repository.py -v
"""
from __future__ import annotations

import logging

from custom_components.nem_pd7day.json_repository import JsonRepository
from ha_free import assert_imports_without_home_assistant
from support import run_async

MESSAGE = "Migrating things from legacy storage key to nem_pd7day.%s.things"


class _Store:
    def __init__(self, data=None) -> None:
        self.data = data
        self.saved: list = []

    async def async_load(self):
        return self.data

    async def async_save(self, data) -> None:
        self.saved.append(data)
        self.data = data


def _never():
    raise AssertionError("the legacy store must not be built")


def test_imports_without_home_assistant():
    assert_imports_without_home_assistant("json_repository")


def test_scoped_data_is_returned_and_the_legacy_store_never_built():
    store = _Store({"a": 1})
    assert run_async(JsonRepository(store, _never, MESSAGE).load("QLD1")) == {"a": 1}
    assert store.saved == []


def test_falsy_scoped_data_that_is_not_none_is_returned_as_is():
    store = _Store({})
    assert run_async(JsonRepository(store, _never, MESSAGE).load("QLD1")) == {}


def test_legacy_data_is_saved_under_the_scoped_key_and_logged(caplog):
    store = _Store(None)
    legacy = _Store({"legacy": True})
    repo = JsonRepository(store, lambda: legacy, MESSAGE)
    with caplog.at_level(logging.INFO):
        data = run_async(repo.load("NSW1"))
    assert data == {"legacy": True}
    assert store.saved == [{"legacy": True}]
    assert legacy.data == {"legacy": True}, "the legacy key is left in place"
    records = [r for r in caplog.records if r.name.startswith("custom_components.nem_pd7day")]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, "Migrating things from legacy storage key to nem_pd7day.nsw1.things"),
    ]


def test_empty_legacy_data_is_not_migrated(caplog):
    for legacy_value in (None, {}):
        store = _Store(None)
        with caplog.at_level(logging.INFO):
            data = run_async(JsonRepository(store, lambda: _Store(legacy_value), MESSAGE).load("QLD1"))
        assert data is None
        assert store.saved == []
    assert not [r for r in caplog.records if "Migrating" in r.getMessage()]


def test_save_passes_the_document_through():
    store = _Store()
    payload = {"forecast_history": {}}
    run_async(JsonRepository(store, _never, MESSAGE).save(payload))
    assert store.saved == [payload] and store.saved[0] is payload
