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

    # A single DataFrame holds every series, all sharing one date index so they
    # line up row-for-row. Columns: qqq (index proxy), tqqq (synthetic leveraged
    # ETF), rf (annualised risk-free rate), spx (S&P 500 / VOO proxy).
    frame: pd.DataFrame

    @property
    def index(self) -> pd.DatetimeIndex:
        # Convenience accessor so callers can write md.index instead of
        # md.frame.index — the two are identical.
        return self.frame.index

    def slice(self, start: str | None = None, end: str | None = None) -> "MarketData":
        # Return a NEW MarketData restricted to [start, end]; the original is
        # left untouched (callers slice to different windows without side effects).
        f = self.frame                                   # start from the full frame
        if start is not None:                            # a lower bound was given
            f = f[f.index >= pd.Timestamp(start)]        # keep rows on/after start
        if end is not None:                              # an upper bound was given
            f = f[f.index <= pd.Timestamp(end)]          # keep rows on/before end
        # .copy() so the sliced frame is independent of the parent (avoids pandas
        # SettingWithCopy surprises if the caller later mutates it).
        return MarketData(f.copy())


def _load_ndx(path: Path) -> pd.Series:
    """Load the Nasdaq-100 index close as an ascending float series."""
    raw = pd.read_csv(path)                              # read the raw CSV as-is
    # The investing.com export quotes the close under "Price" with thousands
    # separators ("20,123.45"). Force to text -> strip commas -> parse float, in
    # that order, so pandas doesn't misinfer the dtype on the comma-formatted
    # strings.
    price = (
        raw["Price"].astype(str)                         # ensure string dtype
        .str.replace(",", "", regex=False)               # "20,123.45" -> "20123.45"
        .astype(float)                                   # now safe to parse as float
    )
    dates = pd.to_datetime(raw["Date"])                  # parse the Date column to timestamps
    # Pair prices with dates into a Series, then sort ascending (the export is
    # newest-first; every downstream calc assumes oldest-first).
    series = pd.Series(price.values, index=dates, name="ndx").sort_index()
    # A refresh can re-append today's row; drop duplicate dates, keeping the
    # last (freshest) occurrence so there's exactly one row per trading day.
    series = series[~series.index.duplicated(keep="last")]
    return series


def _load_riskfree(path: Path) -> pd.Series:
    """Load DGS3MO (percent) as a daily decimal-rate series, forward-filled."""
    raw = pd.read_csv(path)                              # read the FRED CSV
    # FRED quotes the rate in percent (e.g. 5.25); divide by 100 to a decimal
    # (0.0525). errors="coerce" turns any non-numeric cell into NaN rather than
    # raising — FRED uses "." for missing days.
    rate = pd.to_numeric(raw["DGS3MO"], errors="coerce") / 100.0
    dates = pd.to_datetime(raw["observation_date"])      # FRED's date column name
    series = pd.Series(rate.values, index=dates, name="rf").sort_index()  # ascending
    series = series[~series.index.duplicated(keep="last")]  # one row per date
    # FRED leaves market holidays blank (now NaN); carry the last known rate
    # forward so every trading day has a usable rate.
    series = series.ffill()
    return series


def _load_spx(path: Path) -> pd.Series:
    """Load the S&P 500 index close (same CSV shape as NDX) as a float series.

    This is the underlying VOO tracks; used for the overheated "ballast" sleeve.
    """
    raw = pd.read_csv(path)                              # read the CSV
    # Same comma-quoted "Price" format as NDX; strip commas then parse to float.
    price = raw["Price"].astype(str).str.replace(",", "", regex=False).astype(float)
    dates = pd.to_datetime(raw["Date"])                  # parse dates
    series = pd.Series(price.values, index=dates, name="spx").sort_index()  # ascending
    return series[~series.index.duplicated(keep="last")]  # de-dupe, keep freshest


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
    # Resolve each input path: use the caller's override if given, else the
    # bundled default under <project>/data. SPX has no override (always bundled).
    ndx_path = Path(ndx_path) if ndx_path else DATA_DIR / "NDX.csv"
    tbill_path = Path(tbill_path) if tbill_path else DATA_DIR / "tbill_dgs3mo.csv"
    spx_path = DATA_DIR / "SPX.csv"

    # Refresh the index data on demand. Import fetch_data ONLY when actually
    # updating: it pulls in urllib and isn't available/usable in restricted
    # environments like the browser (Pyodide), where auto_update is always off
    # and the CSVs are pre-mounted. This keeps the offline path dependency-free.
    if auto_update:
        from fetch_data import ensure_ndx_csv, ensure_spx_csv

        ensure_ndx_csv(ndx_path, max_age_days=max_age_days, auto_update=True)
        ensure_spx_csv(spx_path, max_age_days=max_age_days, auto_update=True)

    ndx = _load_ndx(ndx_path)                            # Nasdaq-100 index (ascending)
    rf = _load_riskfree(tbill_path)                      # 3-month T-bill, daily decimal
    spx = _load_spx(spx_path)                            # S&P 500 index (VOO source)

    # The index defines our trading calendar. Reindex the T-bill onto exactly
    # those dates: ffill carries the last rate over holidays, bfill covers any
    # gap before the first rate observation, so rf has no NaNs.
    rf = rf.reindex(ndx.index).ffill().bfill()

    # QQQ proxy: normalise the index to a $100 start so levels read like an ETF
    # share price and are directly comparable to the synthetic TQQQ below.
    qqq = ndx / ndx.iloc[0] * 100.0
    qqq.name = "qqq"

    # VOO proxy: the S&P 500 index aligned to the NDX calendar (ffill/bfill any
    # missing dates), then normalised to the same $100 start. This is the
    # ballast sleeve that overheated deposits flow into (contributions mode).
    spx = spx.reindex(ndx.index).ffill().bfill()
    voo = spx / spx.iloc[0] * 100.0
    voo.name = "spx"

    # Index daily simple return, r_t = price_t / price_{t-1} - 1. First row has
    # no prior day, so pct_change yields NaN -> fill with 0 (flat day one).
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

    # Assemble the four aligned series into one DataFrame (shared date index).
    frame = pd.DataFrame(
        {
            "qqq": qqq,      # index proxy, $100 base — the signal asset
            "tqqq": tqqq,    # synthetic leveraged ETF, $100 base
            "rf": rf,        # annualised risk-free decimal rate (cash sleeve)
            "spx": voo,      # S&P 500 (VOO) proxy, $100 base (ballast sleeve)
        }
    )
    # Drop any row with a NaN in any column (e.g. the MA warmup edge) so every
    # row is fully populated before the backtest consumes it.
    frame = frame.dropna()
    return MarketData(frame)


# Running this file directly is a quick smoke test of the data pipeline: build
# everything and print the shape/range plus the first and last few rows.
if __name__ == "__main__":
    md = build_market_data()
    print(f"Rows: {len(md.frame)}")
    print(f"Range: {md.index.min().date()} -> {md.index.max().date()}")
    print(md.frame.head())
    print(md.frame.tail())
