"""Tests for startup_trace.StartupTrace, the setup phase timer.

Startup slowness kept turning out to be somewhere other than where it looked
(the PD7DAY parse was blamed and moved to the executor; the real cost was
per-region duplication, then the market notice fetch), so setup is measured
rather than guessed at. These pin what a reader of the log can rely on.
"""

from __future__ import annotations

import logging

import pytest

from support import load

_st = load("startup_trace")
StartupTrace = _st.StartupTrace


def test_records_phases_in_order():
    trace = StartupTrace("QLD1")
    trace.checkpoint("first")
    trace.checkpoint("second")
    assert [name for name, _ in trace.phases] == ["first", "second"]
    assert trace.total >= 0


def test_phase_records_even_when_block_raises():
    """A failed setup still reports where the time went."""
    trace = StartupTrace("QLD1")
    with pytest.raises(ValueError):
        with trace.phase("blocking fetch"):
            raise ValueError("NEMWEB down")
    assert [name for name, _ in trace.phases] == ["blocking fetch"]


def test_summary_orders_slowest_first():
    trace = StartupTrace("QLD1")
    trace.phases = [("fast", 0.002), ("slowest", 1.5), ("middle", 0.4)]
    summary = trace.summary()
    assert summary.index("slowest") < summary.index("middle") < summary.index("fast")


def test_logs_slow_phase_at_info(caplog):
    """Slow phases surface without needing debug logging enabled."""
    logger = logging.getLogger("test_startup_trace_slow")
    trace = StartupTrace("QLD1", logger)
    trace._last -= 2.0  # pretend the previous phase started 2 s ago
    with caplog.at_level(logging.INFO, logger=logger.name):
        trace.checkpoint("slow thing")
    assert any(r.levelno == logging.INFO for r in caplog.records)
