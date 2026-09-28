"""Data loading and synthetic ETF construction for the AGITQ 161-day MA strategy.

The workspace ships two raw inputs:

* ``NDX.csv`` - daily Nasdaq-100 index (from investing.com), quoted numbers with
  thousands separators and rows ordered newest-first.
* ``tbill_dgs3mo.csv`` - FRED DGS3MO, the 3-month T-bill rate in percent.

From the Nasdaq-100 index we build:

* ``QQQ`` - an unleveraged proxy that tracks the index 1:1 (the signal asset).
* ``TQQQ`` - a synthetic 3x daily-reset ETF, modelled from the index daily
  return with expense ratio and financing (borrow) cost, which is what a real
  leveraged ETF pays on its swap-financed exposure.
* ``SPX`` proxy is *not* derived here; the strategy's "ballast" S&P 500 sleeve is
  approximated with the risk-free (T-bill) series when no S&P data is supplied,
  and the backtest treats the SPY-hold benchmark separately (see ``backtest``).

Modelling choices (documented so they can be tuned):

* TQQQ leverage ``L = 3``.
* TQQQ expense ratio ``0.95%`` annualised (ProShares TQQQ).
* Financing spread over the 3-month T-bill on the borrowed ``(L - 1) = 2x``
  notional, **calibrated to -0.40%** against real TQQQ (see ``calibrate.py``).

Synthetic daily TQQQ return:

    r_tqqq = L * r_index - (L - 1) * (rf + spread) / 252 - expense / 252

This is the standard leveraged-ETF path model. It intentionally omits
bid/ask and rebalancing slippage, which are second order for a daily-checked
trend strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# Default location of the bundled data files (``<project>/data``).
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# --- TQQQ modelling constants -------------------------------------------------
TQQQ_LEVERAGE = 3.0
TQQQ_EXPENSE_RATIO = 0.0095      # 0.95% annual
# Financing spread over the T-bill on the borrowed (L-1)x notional. Calibrated
# against real split-adjusted TQQQ over 2010-2026 (src/calibrate.py): the value
# that minimises daily-return RMSE and zeroes residual annual drift is ~-0.40%.
# A negative spread means real TQQQ tracked 3x the index slightly better than a
# naive T-bill borrow cost implies (securities-lending income, favourable swap
# terms). Override with build_market_data(financing_spread=...) to stress test.
FINANCING_SPREAD = -0.0040       # -0.40%, empirically calibrated
TRADING_DAYS = 252


@dataclass
class MarketData:
    """Aligned daily series indexed by date (ascending)."""

    frame: pd.DataFrame  # columns: qqq, tqqq, rf (daily risk-free), spx (VOO proxy)

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.frame.index

    def slice(self, start: str | None = None, end: str | None = None) -> "MarketData":
        f = self.frame
        if start is not None:
            f = f[f.index >= pd.Timestamp(start)]
        if end is not None:
            f = f[f.index <= pd.Timestamp(end)]
        return MarketData(f.copy())


def _load_ndx(path: Path) -> pd.Series:
    """Load the Nasdaq-100 index close as an ascending float series."""
    raw = pd.read_csv(path)
    # The investing.com export quotes the close under "Price" with thousands
    # separators ("20,123.45"). Force to text -> strip commas -> parse float, in
    # that order, so pandas doesn't misinfer the dtype on the comma-formatted
    # strings.
    price = (
        raw["Price"].astype(str).str.replace(",", "", regex=False).astype(float)
    )
    dates = pd.to_datetime(raw["Date"])
    series = pd.Series(price.values, index=dates, name="ndx").sort_index()
    # Drop any duplicate dates, keep last.
    series = series[~series.index.duplicated(keep="last")]
    return series


def _load_riskfree(path: Path) -> pd.Series:
    """Load DGS3MO (percent) as a daily decimal-rate series, forward-filled."""
    raw = pd.read_csv(path)
    rate = pd.to_numeric(raw["DGS3MO"], errors="coerce") / 100.0
    dates = pd.to_datetime(raw["observation_date"])
    series = pd.Series(rate.values, index=dates, name="rf").sort_index()
    series = series[~series.index.duplicated(keep="last")]
    # FRED leaves holidays blank; carry the last known rate forward.
    series = series.ffill()
    return series


def _load_spx(path: Path) -> pd.Series:
    """Load the S&P 500 index close (same CSV shape as NDX) as a float series.

    This is the underlying VOO tracks; used for the overheated "ballast" sleeve.
    """
    raw = pd.read_csv(path)
    price = raw["Price"].astype(str).str.replace(",", "", regex=False).astype(float)
    dates = pd.to_datetime(raw["Date"])
    series = pd.Series(price.values, index=dates, name="spx").sort_index()
    return series[~series.index.duplicated(keep="last")]


def build_market_data(
    ndx_path: Path | str | None = None,
    tbill_path: Path | str | None = None,
    leverage: float = TQQQ_LEVERAGE,
    expense_ratio: float = TQQQ_EXPENSE_RATIO,
    financing_spread: float = FINANCING_SPREAD,
    auto_update: bool = True,
    max_age_days: float | None = None,
) -> MarketData:
    """Build aligned QQQ / synthetic-TQQQ / risk-free daily series.

    When ``auto_update`` is True (default), the NDX data file is downloaded if
    it is missing or stale before loading. Staleness is close-aware by default
    (``max_age_days=None``): it refreshes once the latest trading session's
    close has passed and that bar is missing. Pass a number to ``max_age_days``
    for a simple calendar-day rule instead. A failed refresh falls back to the
    cached CSV; only a missing file with no network is fatal. Set
    ``auto_update=False`` for fully offline / reproducible runs.
    """
    ndx_path = Path(ndx_path) if ndx_path else DATA_DIR / "NDX.csv"
    tbill_path = Path(tbill_path) if tbill_path else DATA_DIR / "tbill_dgs3mo.csv"
    spx_path = DATA_DIR / "SPX.csv"

    # Refresh the index data on demand (no-op if fresh or auto_update disabled).
    from fetch_data import ensure_ndx_csv, ensure_spx_csv

    # When auto_update is off this is just a cached-file re-read (e.g. secondary
    # per-leverage builds); stay quiet so the log isn't repeated.
    _verbose = auto_update
    ensure_ndx_csv(ndx_path, max_age_days=max_age_days, auto_update=auto_update, verbose=_verbose)
    ensure_spx_csv(spx_path, max_age_days=max_age_days, auto_update=auto_update, verbose=_verbose)

    ndx = _load_ndx(ndx_path)
    rf = _load_riskfree(tbill_path)
    spx = _load_spx(spx_path)

    # Align risk-free onto the trading calendar defined by the index.
    rf = rf.reindex(ndx.index).ffill().bfill()

    # QQQ proxy: normalise the index to a $100 start so levels are ETF-like.
    qqq = ndx / ndx.iloc[0] * 100.0
    qqq.name = "qqq"

    # VOO proxy: the S&P 500 index normalised to $100, aligned to the NDX
    # calendar (forward-filled across any missing dates). This is the ballast
    # sleeve overheated deposits flow into.
    spx = spx.reindex(ndx.index).ffill().bfill()
    voo = spx / spx.iloc[0] * 100.0
    voo.name = "spx"

    # Index daily simple return.
    r_index = ndx.pct_change().fillna(0.0)

    # Synthetic TQQQ daily return with financing + expense drag (the standard
    # daily-reset leveraged-ETF path model).
    #   - A 3x ETF holds 1x in cash and BORROWS the other (leverage-1)=2x. It
    #     pays interest on that borrowed notional at the T-bill rate plus the
    #     financing spread, divided by 252 to get the daily drag.
    daily_financing = (leverage - 1.0) * (rf + financing_spread) / TRADING_DAYS
    #   - The 0.95% annual fund fee, also spread across the trading year.
    daily_expense = expense_ratio / TRADING_DAYS
    #   - 3x the index's daily move, minus the two drags. Because this compounds
    #     DAILY returns below, volatility drag emerges automatically: an up-then-
    #     down index leaves 3x TQQQ below 3x the index's net move, just like the
    #     real product. (Omits bid/ask + rebalancing slippage: second-order here.)
    r_tqqq = leverage * r_index - daily_financing - daily_expense
    r_tqqq.iloc[0] = 0.0  # no prior day on day one -> flat, not NaN

    # Compound the daily returns into a price path, same $100 base as QQQ so the
    # two curves are directly comparable on a chart.
    tqqq = (1.0 + r_tqqq).cumprod() * 100.0
    tqqq.name = "tqqq"

    # Daily risk-free growth factor for the SGOV/cash sleeve.
    frame = pd.DataFrame(
        {
            "qqq": qqq,
            "tqqq": tqqq,
            "rf": rf,  # annualised decimal rate
            "spx": voo,  # S&P 500 (VOO) proxy, $100 base
        }
    )
    frame = frame.dropna()
    return MarketData(frame)


if __name__ == "__main__":
    md = build_market_data()
    print(f"Rows: {len(md.frame)}")
    print(f"Range: {md.index.min().date()} -> {md.index.max().date()}")
    print(md.frame.head())
    print(md.frame.tail())
