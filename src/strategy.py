"""AGITQ 161-day moving-average TQQQ strategy.

Rules (as described in the r/TQQQ "ultimate TQQQ strategy" post):

* Signal asset is QQQ (the Nasdaq-100 proxy), not TQQQ.
* When QQQ closes **above** its 161-day simple moving average -> hold TQQQ.
* When QQQ closes **below** its 161-day MA -> exit to treasury (SGOV/cash).
* Overheated / ballast rule: when QQQ is more than ``+5%`` above the 161-day MA,
  do **not** add new money to TQQQ. New contributions go into an S&P 500 sleeve
  instead. (This only affects deposits; existing TQQQ keeps riding.)
* Checked once per trading day; a signal acts on the next close.

This module produces a per-day target allocation. The backtest engine consumes
it to compute equity paths for both a lump-sum and a periodic-contribution mode.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MA_WINDOW = 161
OVERHEATED_THRESHOLD = 0.05  # +5% above the MA


@dataclass
class Signals:
    """Daily signal frame produced by :func:`compute_signals`."""

    frame: pd.DataFrame  # columns: ma, above, overheated, target

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.frame.index


def compute_signals(
    qqq: pd.Series,
    ma_window: int = MA_WINDOW,
    overheated_threshold: float | None = OVERHEATED_THRESHOLD,
) -> Signals:
    """Compute the daily 161-MA signal from the QQQ price series.

    Returns a frame with:

    * ``ma``          - the trailing simple moving average of QQQ.
    * ``above``       - True when QQQ close > MA (invested-in-TQQQ regime).
    * ``overheated``  - True when QQQ close > MA * (1 + threshold). Always False
      when ``overheated_threshold`` is ``None`` (the ballast rule disabled).
    * ``target``      - held-capital target: ``"TQQQ"`` when above, else
      ``"CASH"`` (SGOV). Contributions are routed separately in the backtest.

    Passing ``overheated_threshold=None`` disables the +5% overheated/ballast
    rule entirely: contributions above the MA always buy TQQQ (no S&P sleeve).

    The signal is computed on the close and, to avoid look-ahead, the backtest
    applies it starting the following trading day.
    """
    ma = qqq.rolling(window=ma_window, min_periods=ma_window).mean()
    above = qqq > ma
    if overheated_threshold is None:
        overheated = pd.Series(False, index=qqq.index)
    else:
        overheated = qqq > ma * (1.0 + overheated_threshold)

    target = np.where(above, "TQQQ", "CASH")

    frame = pd.DataFrame(
        {
            "qqq": qqq,
            "ma": ma,
            "above": above,
            "overheated": overheated,
            "target": target,
        }
    )
    # Only meaningful once the MA warms up.
    frame = frame.dropna(subset=["ma"])
    return Signals(frame)
