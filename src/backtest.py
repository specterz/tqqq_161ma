"""Backtest engine for the AGITQ 161-day MA strategy.

Supports two modes:

* ``lump_sum``: invest a fixed amount on day one, let the strategy rotate
  between TQQQ and cash (SGOV). Best for a clean strategy-vs-benchmark read.
* ``contributions``: invest a fixed amount every N trading days (dollar-cost
  averaging). This is where the overheated "ballast" rule bites: when QQQ is
  >5% over its 161-MA, new deposits go to the S&P 500 sleeve instead of TQQQ.

Benchmarks computed alongside the strategy:

* ``TQQQ hold`` - buy-and-hold synthetic TQQQ.
* ``SPY hold``  - buy-and-hold the S&P 500 sleeve.

Metrics: total return, annualised (CAGR) return, max drawdown, trade count,
and total commission paid.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from data import MarketData, TRADING_DAYS
from strategy import Signals, compute_signals

DEFAULT_COMMISSION = 0.0  # per round trip $; overridable


@dataclass
class BacktestResult:
    # One flat record per backtest run; every engine (lump-sum, hold, DCA)
    # returns this same shape so the table/chart code can treat them uniformly.
    equity: pd.Series           # dated equity curve (portfolio $ per trading day)
    invested: float             # total cash put in (initial + all deposits)
    final_value: float          # equity on the last day
    total_return: float         # final/invested - 1, a raw multiple (not annualised)
    annualized: float           # money-weighted (IRR) or geometric CAGR
    annualized_kind: str        # "IRR" or "CAGR" — which of the two above is stored
    max_drawdown: float         # worst peak-to-trough dip (negative decimal)
    entry_trades: int = 0       # signal buys: QQQ crossed above the MA -> TQQQ
    exit_trades: int = 0        # signal sells: QQQ crossed below the MA -> cash
    contrib_trades: int = 0     # scheduled deposits (contributions mode)
    commission_paid: float = 0.0
    label: str = ""
    sharpe: float = float("nan")  # annualized Sharpe ratio of daily returns
    daily_returns: pd.Series | None = None  # pure daily investment returns (DCA)

    @property
    def trades(self) -> int:
        """Total signal rotations (entries + exits); deposits excluded."""
        # Deposits aren't "trades" for turnover purposes — only regime rotations.
        return self.entry_trades + self.exit_trades

    def monthly_returns(self) -> pd.Series:
        """Calendar-month returns as a Series indexed by month-end.

        Uses the pure investment-return series when available (DCA mode, where
        the equity curve is contaminated by deposits); otherwise compounds the
        equity curve's daily returns. Either way this is the month's investment
        performance, comparable to a monthly-returns table.
        """
        if self.daily_returns is not None:
            rets = self.daily_returns.dropna()
        else:
            rets = self.equity.pct_change().dropna()
        # Compound daily returns within each calendar month.
        grouped = (1.0 + rets).groupby([rets.index.year, rets.index.month]).prod() - 1.0
        return grouped

    def summary(self) -> str:
        # One-line human summary for stdout/logs. `self.sharpe != self.sharpe`
        # is the NaN test (NaN is the only value not equal to itself) — DCA and
        # any rf-less curve may carry NaN Sharpe, shown as "n/a".
        sharpe = "n/a" if self.sharpe != self.sharpe else f"{self.sharpe:.2f}"
        return (
            f"{self.label:<16} invested=${self.invested:,.0f} "
            f"final=${self.final_value:,.0f} "
            f"total={self.total_return * 100:+.1f}% "
            f"{self.annualized_kind}={self.annualized * 100:+.1f}% "
            f"Sharpe={sharpe} "
            f"maxDD={self.max_drawdown * 100:.1f}% "
            f"entries={self.entry_trades} exits={self.exit_trades} "
            f"contribs={self.contrib_trades} comm=${self.commission_paid:,.0f}"
        )


def format_results_table(results: list["BacktestResult"]) -> str:
    """Render a list of results as an aligned, boxed text table.

    Columns are right-aligned numbers with a left-aligned label. The annualized
    header adapts to the metric in use (CAGR for lump-sum, IRR for DCA); if the
    results mix both kinds the header falls back to "Ann.".
    """
    # If every result uses the same annualisation (all CAGR or all IRR) name the
    # column accordingly; a mixed set can't be labelled honestly -> generic "Ann.".
    kinds = {r.annualized_kind for r in results}
    ann_header = kinds.pop() if len(kinds) == 1 else "Ann."

    headers = ["Strategy", "Invested", "Final", "Total", ann_header, "Sharpe",
               "MaxDD", "Entries", "Exits", "Contribs", "Comm"]
    rows = []
    for r in results:
        # `x != x` is the NaN guard again; format everything else as signed %.
        ann = "n/a" if r.annualized != r.annualized else f"{r.annualized * 100:+.1f}%"
        sharpe = "n/a" if r.sharpe != r.sharpe else f"{r.sharpe:.2f}"
        rows.append([
            r.label,
            f"${r.invested:,.0f}",
            f"${r.final_value:,.0f}",
            f"{r.total_return * 100:+.1f}%",
            ann,
            sharpe,
            f"{r.max_drawdown * 100:.1f}%",
            f"{r.entry_trades:,}",
            f"{r.exit_trades:,}",
            f"{r.contrib_trades:,}",
            f"${r.commission_paid:,.0f}",
        ])

    # Column widths from the widest cell (header or any row).
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))  # grow to fit the longest cell

    def fmt_row(cells: list[str]) -> str:
        out = [cells[0].ljust(widths[0])]  # label left-aligned, numbers right-aligned
        out += [cells[i].rjust(widths[i]) for i in range(1, len(cells))]
        return "| " + " | ".join(out) + " |"

    # Build the box: a "+---+" rule line reused above/below the header and at the
    # foot; w+2 accounts for the one-space padding on each side of every cell.
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [sep, fmt_row(headers), sep]
    lines += [fmt_row(row) for row in rows]
    lines.append(sep)
    return "\n".join(lines)


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_monthly_table(result: "BacktestResult") -> str:
    """Year x month return grid (percent) with a YTD column, boxed with column
    separators to match the results table. An Avg footer row is included.
    """
    m = result.monthly_returns()          # Series indexed by (year, month)
    if m.empty:
        return "(no monthly data)"

    years = sorted({y for (y, _mo) in m.index})  # one grid row per calendar year
    headers = ["Year"] + _MONTHS + ["YTD"]

    def pct(v: float) -> str:
        return "" if v != v else f"{v * 100:+.1f}%"  # v!=v catches NaN

    # Per-column accumulators for the Avg footer (mean per calendar month).
    col_sums = [0.0] * 12
    col_counts = [0] * 12
    ytd_sum = 0.0
    ytd_count = 0

    body_rows = []
    for y in years:
        row = [str(y)]
        month_vals = []                    # only the months that actually exist this year
        for mo in range(1, 13):
            if (y, mo) in m.index:
                v = m.loc[(y, mo)]
                month_vals.append(v)
                col_sums[mo - 1] += v       # feed the per-month Avg accumulators
                col_counts[mo - 1] += 1
                row.append(pct(v))
            else:
                row.append("")             # blank cell for a month with no data
        # YTD compounds the year's monthly returns multiplicatively (not a sum):
        # (1+r1)(1+r2)... - 1. Empty year -> NaN so it renders blank.
        ytd = (np.prod([1.0 + v for v in month_vals]) - 1.0) if month_vals else float("nan")
        if ytd == ytd:                     # only average real (non-NaN) YTDs
            ytd_sum += ytd
            ytd_count += 1
        row.append(pct(ytd))
        body_rows.append(row)

    # Footer: arithmetic mean of each month across all years (guard divide-by-zero
    # for months that never appeared), plus the mean of the yearly YTD figures.
    avg_row = ["Avg"] + [
        pct(col_sums[mo] / col_counts[mo]) if col_counts[mo] else "" for mo in range(12)
    ] + [pct(ytd_sum / ytd_count) if ytd_count else ""]

    # Column widths from the widest cell in each column (header, body, footer).
    all_rows = [headers] + body_rows + [avg_row]  # consider every row together
    ncol = len(headers)
    widths = [0] * ncol
    for row in all_rows:
        for i in range(ncol):
            widths[i] = max(widths[i], len(row[i]))

    def fmt_row(cells: list[str]) -> str:
        out = [cells[0].ljust(widths[0])]                       # Year left-aligned
        out += [cells[i].rjust(widths[i]) for i in range(1, ncol)]
        return "| " + " | ".join(out) + " |"

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [f"Monthly returns — {result.label}", sep, fmt_row(headers), sep]
    lines += [fmt_row(r) for r in body_rows]
    lines.append(sep)
    lines.append(fmt_row(avg_row))
    lines.append(sep)
    lines.append("Note: Avg = mean return per calendar month (the YTD column is "
                 "the compounded year-to-date return).")
    return "\n".join(lines)


def _cagr(equity: pd.Series) -> float:
    """Geometric compound annual growth rate of an equity curve.

    Valid only for a **single lump sum** invested on day one (no later flows).
    For a curve fed by ongoing contributions this is meaningless -- use
    :func:`xirr` on the dated cash flows instead.
    """
    # Elapsed time in years via actual calendar days / 365.25 (accounts for leap
    # years); using trading-day counts would understate the horizon.
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:                       # single-day (or reversed) curve: no growth to annualise
        return 0.0
    start = equity.iloc[0]
    end = equity.iloc[-1]
    if start <= 0:                       # can't take a ratio off a non-positive base
        return np.nan
    # Geometric CAGR: the constant yearly rate that turns `start` into `end`.
    return (end / start) ** (1.0 / years) - 1.0


def xirr(dates: list[pd.Timestamp], amounts: list[float]) -> float:
    """Money-weighted annualised return (XIRR) for dated cash flows.

    Sign convention: contributions/deposits are **negative** (cash leaving your
    pocket), and the final portfolio value is a single **positive** flow. Solves
    ``sum(amount_i / (1 + r) ** years_i) == 0`` for ``r`` via bisection, which is
    robust and dependency-free (no scipy).

    Returns ``nan`` if a rate cannot be bracketed (e.g. all-loss series).
    """
    # XIRR only has a root when flows change sign. All-negative (only deposits)
    # or all-positive (only payouts) never crosses zero -> undefined, return NaN.
    if not amounts or all(a <= 0 for a in amounts) or all(a >= 0 for a in amounts):
        return float("nan")

    # Discount each flow relative to the FIRST flow's date, measured in years.
    t0 = dates[0]
    years = np.array([(d - t0).days / 365.25 for d in dates], dtype=float)
    amt = np.array(amounts, dtype=float)

    def npv(rate: float) -> float:
        # Net present value of all flows at this annual rate. A rate <= -100%
        # makes the discount base non-positive (undefined powers); return +inf
        # so the bracketing/bisection simply steers away from it.
        base = 1.0 + rate
        if base <= 0:
            return float("inf")
        return float(np.sum(amt / base ** years))

    # Bracket the root between -99.99%/yr and +1000%/yr. NPV is monotonically
    # decreasing in rate, so if it has the SAME sign at both ends it never
    # crosses zero in the bracket -> unsolvable (e.g. all-loss series) -> NaN.
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    # Same sign at both ends (product > 0) means no sign change inside the
    # bracket, so no root to find here -> bail with NaN.
    if np.isnan(f_lo) or np.isnan(f_hi) or f_lo * f_hi > 0:
        return float("nan")

    # Bisection: halve the bracket 200 times, keeping the half whose endpoints
    # straddle the root (opposite signs). Converges to the rate where NPV == 0.
    # 200 iterations is far past double-precision convergence — cheap insurance.
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:  # NPV effectively zero: `mid` is the rate we want
            return mid
        # Keep whichever half still brackets the sign change. NPV decreases with
        # rate, so the product test tells us which side the root fell on.
        if f_lo * f_mid < 0:   # sign flips between lo and mid -> root in [lo, mid]
            hi, f_hi = mid, f_mid
        else:                  # otherwise the root is in [mid, hi]
            lo, f_lo = mid, f_mid
    # Didn't hit the tolerance in 200 steps: return the bracket midpoint anyway.
    return 0.5 * (lo + hi)


def _max_drawdown(equity: pd.Series) -> float:
    # Worst peak-to-trough loss. cummax() is the running high-water mark; equity
    # divided by that high (minus 1) is the drawdown at each point (<= 0). The
    # minimum is the deepest dip ever endured.
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min())


def _sharpe(equity: pd.Series, rf_annual: pd.Series | None = None) -> float:
    """Annualised Sharpe ratio from an equity curve's daily returns.

    Sharpe = mean(excess daily return) / std(daily return) * sqrt(252), where
    the excess return subtracts the daily risk-free rate. If ``rf_annual`` (an
    annualised decimal-rate series aligned to the equity index) is given, it is
    used as the risk-free benchmark; otherwise excess == raw return.

    In DCA / contribution mode the equity curve jumps on each deposit; those
    cash-flow jumps are *not* investment returns and would corrupt the daily
    return series. Sharpe is therefore only meaningful for the lump-sum and
    buy-and-hold curves; contribution results leave it as NaN.
    """
    rets = equity.pct_change().dropna()
    if len(rets) < 2:                    # need at least two returns for a std dev
        return float("nan")
    if rf_annual is not None:
        # De-annualise the risk-free rate to a daily figure, aligned to the
        # return dates, and take the return in EXCESS of it (true Sharpe numerator).
        rf_daily = (rf_annual.reindex(rets.index).ffill().bfill() / TRADING_DAYS)
        excess = rets - rf_daily
    else:
        excess = rets                    # no rf supplied: treat raw returns as excess
    sd = excess.std(ddof=1)              # sample std (ddof=1); population std would flatter Sharpe
    if sd == 0 or sd != sd:              # zero vol or NaN -> ratio undefined
        return float("nan")
    # Annualise a daily Sharpe by sqrt(252): variance scales with time, so its
    # square root (the denominator) scales with sqrt(time).
    return float(excess.mean() / sd * np.sqrt(TRADING_DAYS))


def _sharpe_from_returns(rets: pd.Series, rf_annual: pd.Series | None = None) -> float:
    """Annualised Sharpe from a precomputed daily-return series.

    Used by DCA mode, which tracks the portfolio's pure investment return each
    day (excluding deposit inflows) so a meaningful Sharpe can be reported.
    """
    # Same math as _sharpe but fed a ready-made return series (DCA supplies its
    # deposit-free daily returns here, since its equity curve can't be pct_change'd).
    rets = rets.dropna()
    if len(rets) < 2:
        return float("nan")
    if rf_annual is not None:
        rf_daily = (rf_annual.reindex(rets.index).ffill().bfill() / TRADING_DAYS)
        excess = rets - rf_daily
    else:
        excess = rets
    sd = excess.std(ddof=1)
    if sd == 0 or sd != sd:
        return float("nan")
    return float(excess.mean() / sd * np.sqrt(TRADING_DAYS))


def _daily_rf_factor(rf_annual: pd.Series) -> pd.Series:
    """Convert an annualised rate series into daily growth factors."""
    # e.g. 5% annual -> 1 + 0.05/252 per day; multiply cash balance by this each
    # day to accrue T-bill interest (simple, not compounded intra-year).
    return 1.0 + rf_annual / TRADING_DAYS


def run_lump_sum(
    md: MarketData,
    signals: Signals,
    initial: float = 10_000.0,
    commission: float = DEFAULT_COMMISSION,
    label: str = "161MA Strategy",
) -> BacktestResult:
    """Lump-sum strategy: rotate held capital between TQQQ and cash (SGOV).

    The signal computed on day ``t`` is acted on at the close of day ``t+1`` to
    avoid look-ahead bias.
    """
    # inner join keeps only dates present in BOTH price data and signals (drops
    # the MA warmup window that has no signal yet).
    frame = md.frame.join(signals.frame[["target"]], how="inner")
    tqqq_ret = frame["tqqq"].pct_change().fillna(0.0)  # day-one has no prior -> 0
    cash_factor = _daily_rf_factor(frame["rf"])         # per-day cash growth factor

    # THE LOOK-AHEAD GUARD: today's position is YESTERDAY's signal (shift by 1).
    # A signal computed on day t's close can only be acted on at t+1, so we never
    # trade on information we couldn't have had. Day one has no prior -> cash.
    holding = frame["target"].shift(1).fillna("CASH")

    equity = np.empty(len(frame))
    equity[0] = initial          # seed the curve with the lump sum on day one
    entry_trades = 0
    exit_trades = 0
    commission_paid = 0.0
    prev_hold = holding.iloc[0]  # track regime changes to count/charge rotations

    # Pull raw numpy arrays out of the pandas objects: the tight loop below
    # indexes by position thousands of times, and array access is far cheaper
    # than per-element .iloc on a Series.
    tqqq_r = tqqq_ret.values
    cash_f = cash_factor.values
    hold_arr = holding.values

    for i in range(1, len(frame)):
        h = hold_arr[i]
        # Compound yesterday's equity by whichever sleeve we're holding today.
        if h == "TQQQ":
            equity[i] = equity[i - 1] * (1.0 + tqqq_r[i])
        else:
            equity[i] = equity[i - 1] * cash_f[i]
        if h != prev_hold:       # regime flipped since yesterday -> a rotation
            # Rotating INTO TQQQ is an entry; rotating OUT to cash is an exit.
            if h == "TQQQ":
                entry_trades += 1
            else:
                exit_trades += 1
            commission_paid += commission
            equity[i] -= commission   # commission comes straight off equity
            prev_hold = h

    eq = pd.Series(equity, index=frame.index, name="strategy")
    return BacktestResult(
        equity=eq,
        invested=initial,
        final_value=float(eq.iloc[-1]),
        total_return=float(eq.iloc[-1] / initial - 1.0),
        annualized=_cagr(eq),
        annualized_kind="CAGR",
        max_drawdown=_max_drawdown(eq),
        entry_trades=entry_trades,
        exit_trades=exit_trades,
        commission_paid=commission_paid,
        label=label,
        sharpe=_sharpe(eq, frame["rf"]),
    )


def run_hold(
    md: MarketData,
    signals: Signals,
    asset: str,
    initial: float = 10_000.0,
    label: str = "",
) -> BacktestResult:
    """Lump-sum buy-and-hold benchmark for a price column (``tqqq`` or ``qqq``)."""
    # Join to signals only to share the exact same date window as the strategy,
    # so the benchmark curve lines up day-for-day for a fair comparison.
    frame = md.frame.join(signals.frame[["target"]], how="inner")
    price = frame[asset]
    # Buy-and-hold: scale the price path so day one equals the lump sum.
    eq = price / price.iloc[0] * initial
    eq.name = label or asset
    return BacktestResult(
        equity=eq,
        invested=initial,
        final_value=float(eq.iloc[-1]),
        total_return=float(eq.iloc[-1] / initial - 1.0),
        annualized=_cagr(eq),
        annualized_kind="CAGR",
        max_drawdown=_max_drawdown(eq),
        entry_trades=1,   # one buy at inception; buy-and-hold never sells
        exit_trades=0,
        commission_paid=0.0,
        label=label or asset.upper(),
        sharpe=_sharpe(eq, frame["rf"]),
    )


def run_hold_dca(
    md: MarketData,
    signals: Signals,
    asset: str,
    contribution: float = 100.0,
    every_n_days: int = 21,
    initial_lump_sum: float = 0.0,
    label: str = "",
) -> BacktestResult:
    """Dollar-cost-averaging buy-and-hold benchmark into a single asset.

    Uses the **same deposit schedule** as :func:`run_contributions` so the
    money-weighted (IRR) comparison against the strategy is apples-to-apples.
    An optional ``initial_lump_sum`` is invested on day one (also in ``asset``).
    """
    frame = md.frame.join(signals.frame[["target"]], how="inner")  # shared date window
    price = frame[asset].values
    idx = frame.index

    shares = 0.0          # accumulated share count (fractional shares allowed)
    invested = 0.0        # running total of cash deposited
    contrib_trades = 0
    equity = np.empty(len(frame))
    # Dated cash flows for the XIRR: deposits negative, final value positive.
    flow_dates: list[pd.Timestamp] = []
    flow_amounts: list[float] = []

    # Day-one lump sum, invested in the same asset.
    if initial_lump_sum > 0.0:
        shares += initial_lump_sum / price[0]   # buy shares at day-one price
        invested += initial_lump_sum
        contrib_trades += 1
        flow_dates.append(idx[0])
        flow_amounts.append(-initial_lump_sum)  # outflow -> negative

    for i in range(len(frame)):
        # Deposit every N-th trading day (i counts trading days from the start).
        if i % every_n_days == 0:
            shares += contribution / price[i]   # buy more shares at today's price
            invested += contribution
            contrib_trades += 1
            flow_dates.append(idx[i])
            flow_amounts.append(-contribution)
        equity[i] = shares * price[i]           # mark the whole holding to market

    eq = pd.Series(equity, index=idx, name=label or asset)
    flow_dates.append(idx[-1])
    flow_amounts.append(float(eq.iloc[-1]))     # terminal value: the one positive flow

    # For a single held asset the daily investment return is just the asset's
    # own daily price return (deposits buy more shares but don't change it).
    asset_ret = frame[asset].pct_change()

    return BacktestResult(
        equity=eq,
        invested=invested,
        final_value=float(eq.iloc[-1]),
        total_return=float(eq.iloc[-1] / invested - 1.0) if invested else 0.0,
        annualized=xirr(flow_dates, flow_amounts),
        annualized_kind="IRR",
        max_drawdown=_max_drawdown(eq),
        # Buy-and-hold DCA never rotates (no signal entries/exits); every deposit
        # is a contribution trade into the single standing holding.
        entry_trades=0,
        exit_trades=0,
        contrib_trades=contrib_trades,
        commission_paid=0.0,
        label=label or asset.upper(),
        sharpe=_sharpe_from_returns(asset_ret, frame["rf"]),
    )


def run_contributions(
    md: MarketData,
    signals: Signals,
    contribution: float = 100.0,
    every_n_days: int = 21,
    commission: float = 2.0,
    spy_proxy: pd.Series | None = None,
    initial_lump_sum: float = 0.0,
    label: str = "161MA Strategy",
) -> BacktestResult:
    """Dollar-cost-averaging strategy with the overheated ballast rule.

    An optional ``initial_lump_sum`` is deposited on day one and then invested
    following the same strategy rules as any other deposit (TQQQ if above the
    MA and not overheated, ballast if overheated, cash otherwise).

    Every ``every_n_days`` trading days a ``contribution`` is deposited:

    * If QQQ is above its 161-MA and **not** overheated -> buy TQQQ.
    * If QQQ is above the MA but overheated (>+5%) -> buy the S&P 500 sleeve
      (the "ballast").
    * If QQQ is below the MA -> park the deposit in cash (SGOV).

    Held TQQQ is *also* rotated to cash when QQQ drops below the MA (the sell
    rule), and rotated back into TQQQ when it recrosses above (non-overheated).
    """
    # Need the regime flags (above/overheated) here, not just the held target,
    # because deposits route differently from the held-capital rotation.
    frame = md.frame.join(
        signals.frame[["target", "above", "overheated"]], how="inner"
    )
    tqqq_ret = frame["tqqq"].pct_change().fillna(0.0).values
    cash_factor = _daily_rf_factor(frame["rf"]).values

    # VOO / S&P 500 ballast sleeve. Overheated deposits flow here. Prefer the
    # real S&P series (the 'spx' column on MarketData); fall back to an explicit
    # spy_proxy, and only as a last resort to risk-free growth.
    if spy_proxy is not None:
        spy = spy_proxy.reindex(frame.index).ffill()   # align caller series to our dates
        spy_ret = spy.pct_change().fillna(0.0).values
    elif "spx" in frame.columns:
        spy_ret = frame["spx"].pct_change().fillna(0.0).values
    else:
        # No S&P data at all: let the ballast merely earn cash — better than
        # crashing, and the ballast rule is opt-in anyway.
        spy_ret = (cash_factor - 1.0)  # risk-free daily return (last resort)

    # Shift the signal flags by one day, same look-ahead guard as lump-sum:
    # act on day t's signal at t+1. Day one defaults to "below MA" (False).
    above = frame["above"].shift(1).fillna(False).values
    overheated = frame["overheated"].shift(1).fillna(False).values

    # Three sleeves tracked in dollars (not shares): TQQQ, cash/SGOV, and the
    # S&P ballast. Each grows by its own daily return; money moves between them
    # on signal rotations and gets added on deposits.
    tqqq_bal = 0.0
    cash_bal = 0.0
    spy_bal = 0.0

    invested = 0.0
    entry_trades = 0
    exit_trades = 0
    contrib_trades = 0
    commission_paid = 0.0
    equity = np.empty(len(frame))
    # Daily *investment* return of the portfolio, excluding deposit inflows, so
    # a meaningful Sharpe can be computed for DCA mode too.
    daily_ret = np.zeros(len(frame))
    idx = frame.index
    flow_dates: list[pd.Timestamp] = []
    flow_amounts: list[float] = []

    # Day-one lump sum, allocated by the same rules. Because the signal arrays
    # are shifted by one day (look-ahead safe), day 0 counts as "below MA", so
    # the lump sum parks in cash and is deployed into TQQQ on the first day the
    # trend is confirmed above the MA (handled by the re-entry block below).
    if initial_lump_sum > 0.0:
        invested += initial_lump_sum
        contrib_trades += 1
        flow_dates.append(idx[0])
        flow_amounts.append(-initial_lump_sum)   # deposit -> negative flow
        # Route by the same three-way rule used for every deposit (below).
        if above[0] and not overheated[0]:
            tqqq_bal += initial_lump_sum
        elif above[0] and overheated[0]:
            spy_bal += initial_lump_sum
        else:
            cash_bal += initial_lump_sum

    n = len(frame)
    # Daily loop, four steps IN ORDER: (1) grow sleeves, (2) sell rule,
    # (3) re-entry rule, (4) scheduled deposit. Order matters — growth before
    # rotations, rotations before the deposit is measured/added.
    for i in range(n):
        # (1) Grow each sleeve by its daily return first.
        if i > 0:
            tqqq_bal *= (1.0 + tqqq_ret[i])
            cash_bal *= cash_factor[i]
            spy_bal *= (1.0 + spy_ret[i])

        # Sell rule (signal EXIT): if below MA, move ALL TQQQ to cash. Guarded by
        # tqqq_bal > 0 so a prolonged downtrend doesn't book repeated fake exits.
        if not above[i] and tqqq_bal > 0.0:
            cash_bal += tqqq_bal
            tqqq_bal = 0.0
            exit_trades += 1
            cash_bal -= commission        # fee paid out of the newly-cash sleeve
            commission_paid += commission

        # Re-entry (signal ENTRY): if above MA and not overheated, move parked
        # cash into TQQQ. (Deposits parked in a downtrend get deployed on recross.)
        # Note the overheated guard: while >+5% above the MA we hold existing
        # TQQQ but do NOT sweep parked cash in — it waits for a non-overheated day.
        if above[i] and not overheated[i] and cash_bal > 0.0:
            tqqq_bal += cash_bal
            cash_bal = 0.0
            entry_trades += 1
            tqqq_bal -= commission        # fee paid out of the newly-TQQQ sleeve
            commission_paid += commission

        # Portfolio value after market growth + rotations, but BEFORE today's
        # deposit. Comparing this to yesterday's close gives the pure investment
        # return, uncontaminated by the deposit inflow.
        pre_deposit = tqqq_bal + cash_bal + spy_bal
        if i > 0 and equity[i - 1] > 0.0:
            daily_ret[i] = pre_deposit / equity[i - 1] - 1.0

        # Deposit on schedule (a contribution trade, not a signal rotation, so it
        # bumps contrib_trades and never touches entry/exit counts).
        if i % every_n_days == 0:
            invested += contribution
            contrib_trades += 1
            flow_dates.append(idx[i])
            flow_amounts.append(-contribution)
            # Three-way routing = the heart of the ballast rule:
            if above[i] and not overheated[i]:
                tqqq_bal += contribution    # uptrend, not frothy -> buy leverage
            elif above[i] and overheated[i]:
                spy_bal += contribution     # uptrend but >+5% over MA -> ballast
            else:
                cash_bal += contribution    # downtrend -> park in cash, deploy on recross

        equity[i] = tqqq_bal + cash_bal + spy_bal

    eq = pd.Series(equity, index=idx, name="strategy")
    ret_series = pd.Series(daily_ret, index=idx)
    flow_dates.append(idx[-1])
    flow_amounts.append(float(eq.iloc[-1]))
    return BacktestResult(
        equity=eq,
        invested=invested,
        final_value=float(eq.iloc[-1]),
        total_return=float(eq.iloc[-1] / invested - 1.0) if invested else 0.0,
        annualized=xirr(flow_dates, flow_amounts),
        annualized_kind="IRR",
        max_drawdown=_max_drawdown(eq),
        entry_trades=entry_trades,
        exit_trades=exit_trades,
        contrib_trades=contrib_trades,
        commission_paid=commission_paid,
        label=label,
        sharpe=_sharpe_from_returns(ret_series.iloc[1:], frame["rf"]),
        daily_returns=ret_series.iloc[1:],
    )
