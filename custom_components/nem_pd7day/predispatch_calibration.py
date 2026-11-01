"""Pre-dispatch RRP as the timeline serves it: passthrough until stage 3b (#333 decision 3, #337, #339).

Stage 3a records pre-dispatch against the TradingIS actual. Stage 3b will fit
a calibration on those observations and serve it here. Until then the served
price is the RRP itself, labelled ``passthrough``.

Every consumer reads it from this one function, so 3b changes them all at
once and none can drift:

- the price timeline's ``predispatch`` points;
- the node and zone sensors' ``predispatch_forecast`` entries, as
  ``calibrated_rrp_mwh`` beside the raw-RRP price they publish. They stay on
  raw RRP because the local adjustment comes from the same NEMDE run;
- the pre-dispatch constraints sensor's ``calibrated_rrp`` beside ``rrp``.

Domain layer: no Home Assistant imports, no clock.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

PASSTHROUGH = "passthrough"


def calibrated_rrp_mwh(region: str, end: datetime, rrp_mwh: Optional[float]) -> Optional[float]:
    """The served pre-dispatch RRP ($/MWh) for ``region``'s period ending ``end``: the RRP until 3b."""
    return rrp_mwh


def calibrated_source(region: str, end: datetime) -> str:
    """How ``calibrated_rrp_mwh`` served this period: ``passthrough`` until 3b."""
    return PASSTHROUGH
