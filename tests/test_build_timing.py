"""Step timing of a slow attribute build (#363)."""
from __future__ import annotations

import logging
import time

import support

_bt = support.load("build_timing")
LOGGER = logging.getLogger("nem_pd7day.test_build_timing")


def test_a_fast_build_logs_nothing(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger=LOGGER.name)
    timer = _bt.BuildTimer("sensor.fast")
    timer.step("one")
    assert timer.finish(LOGGER) < _bt.SLOW_BUILD_SECONDS
    assert caplog.records == []


def test_a_slow_build_logs_each_step_with_wall_and_cpu(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger=LOGGER.name)
    monkeypatch.setattr(_bt, "SLOW_BUILD_SECONDS", 0.01)
    timer = _bt.BuildTimer("sensor.nsw1_spot")
    time.sleep(0.02)  # waiting, not computing: wall time without CPU time
    timer.step("calibrated memo")
    timer.step("summary")
    wall = timer.finish(LOGGER)
    assert wall >= 0.02
    (record,) = caplog.records
    message = record.getMessage()
    assert record.levelno == logging.DEBUG and message.startswith("sensor.nsw1_spot: attribute build took")
    assert "calibrated memo 0.0" in message and "summary 0.0" in message
    wall_s, cpu_s = (float(message.split(" took ")[1].split(" s wall")[0]),
                     float(message.split(" and ")[1].split(" s CPU")[0]))
    assert cpu_s < wall_s / 2, "a sleep shows as wall time far above CPU: a starved loop"


def test_nothing_is_formatted_unless_debug_is_on(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    monkeypatch.setattr(_bt, "SLOW_BUILD_SECONDS", 0.0)
    timer = _bt.BuildTimer("sensor.x")
    timer.finish(LOGGER)
    assert caplog.records == []


def test_no_steps_still_says_so(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger=LOGGER.name)
    monkeypatch.setattr(_bt, "SLOW_BUILD_SECONDS", 0.0)
    _bt.BuildTimer("sensor.x").finish(LOGGER)
    assert "(no steps)" in caplog.records[0].getMessage()
