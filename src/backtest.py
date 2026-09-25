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
    equity: pd.Series
    invested: float
    final_value: float
    total_return: float
    annualized: float           # money-weighted (IRR) or geometric CAGR
    annualized_kind: str        # "IRR" or "CAGR"
    max_drawdown: float
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
    kinds = {r.annualized_kind for r in results}
    ann_header = kinds.pop() if len(kinds) == 1 else "Ann."

    headers = ["Strategy", "Invested", "Final", "Total", ann_header, "Sharpe",
               "MaxDD", "Entries", "Exits", "Contribs", "Comm"]
    rows = []
    for r in results:
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
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        out = [cells[0].ljust(widths[0])]  # label left-aligned
        out += [cells[i].rjust(widths[i]) for i in range(1, len(cells))]
        return "| " + " | ".join(out) + " |"

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [sep, fmt_row(headers), sep]
    lines += [fmt_row(row) for row in rows]
    lines.append(sep)
    return "\n".join(lines)


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_monthly_table(result: "BacktestResult") -> str:
    """Year x month return grid (percent) with a YTD column, boxed with column
    separators to match the results table. Sum/Avg footer rows included.
    """
    m = result.monthly_returns()
    if m.empty:
        return "(no monthly data)"

    years = sorted({y for (y, _mo) in m.index})
    headers = ["Year"] + _MONTHS + ["YTD"]

    def pct(v: float) -> str:
        return "" if v != v else f"{v * 100:+.1f}%"  # v!=v catches NaN

    # Per-column accumulators for the footer.
    col_sums = [0.0] * 12
    col_counts = [0] * 12
    ytd_sum = 0.0
    ytd_count = 0

    body_rows = []
    for y in years:
        row = [str(y)]
        month_vals = []
        for mo in range(1, 13):
            if (y, mo) in m.index:
                v = m.loc[(y, mo)]
                month_vals.append(v)
                col_sums[mo - 1] += v
                col_counts[mo - 1] += 1
                row.append(pct(v))
            else:
                row.append("")
        ytd = (np.prod([1.0 + v for v in month_vals]) - 1.0) if month_vals else float("nan")
        if ytd == ytd:
            ytd_sum += ytd
            ytd_count += 1
        row.append(pct(ytd))
        body_rows.append(row)

    sum_row = ["Sum"] + [pct(col_sums[mo]) for mo in range(12)] + [pct(ytd_sum)]
    avg_row = ["Avg"] + [
        pct(col_sums[mo] / col_counts[mo]) if col_counts[mo] else "" for mo in range(12)
    ] + [pct(ytd_sum / ytd_count) if ytd_count else ""]

    # Column widths from the widest cell in each column (header, body, footer).
    all_rows = [headers] + body_rows + [sum_row, avg_row]
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
    lines.append(fmt_row(sum_row))
    lines.append(fmt_row(avg_row))
    lines.append(sep)
    lines.append("Note: monthly returns compound; the Sum row is an arithmetic "
                 "total (not a compounded return). Avg = mean per calendar month.")
    return "\n".join(lines)


def _cagr(equity: pd.Series) -> float:
    """Geometric compound annual growth rate of an equity curve.

    Valid only for a **single lump sum** invested on day one (no later flows).
    For a curve fed by ongoing contributions this is meaningless -- use
    :func:`xirr` on the dated cash flows instead.
    """
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return 0.0
    start = equity.iloc[0]
    end = equity.iloc[-1]
    if start <= 0:
        return np.nan
    return (end / start) ** (1.0 / years) - 1.0


def xirr(dates: list[pd.Timestamp], amounts: list[float]) -> float:
    """Money-weighted annualised return (XIRR) for dated cash flows.

    Sign convention: contributions/deposits are **negative** (cash leaving your
    pocket), and the final portfolio value is a single **positive** flow. Solves
    ``sum(amount_i / (1 + r) ** years_i) == 0`` for ``r`` via bisection, which is
    robust and dependency-free (no scipy).

    Returns ``nan`` if a rate cannot be bracketed (e.g. all-loss series).
    """
    if not amounts or all(a <= 0 for a in amounts) or all(a >= 0 for a in amounts):
        return float("nan")

    t0 = dates[0]
    years = np.array([(d - t0).days / 365.25 for d in dates], dtype=float)
    amt = np.array(amounts, dtype=float)

    def npv(rate: float) -> float:
        # Guard against rate <= -100%.
        base = 1.0 + rate
        if base <= 0:
            return float("inf")
        return float(np.sum(amt / base ** years))

    # Bracket the root. NPV is monotonically decreasing in rate.
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if np.isnan(f_lo) or np.isnan(f_hi) or f_lo * f_hi > 0:
        return float("nan")

    for _ in range(200):
        mid = 0.5 * (lo + hi)
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


def _max_drawdown(equity: pd.Series) -> float:
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


def _sharpe_from_returns(rets: pd.Series, rf_annual: pd.Series | None = None) -> float:
    """Annualised Sharpe from a precomputed daily-return series.

    Used by DCA mode, which tracks the portfolio's pure investment return each
    day (excluding deposit inflows) so a meaningful Sharpe can be reported.
    """
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
    frame = md.frame.join(signals.frame[["target"]], how="inner")
    tqqq_ret = frame["tqqq"].pct_change().fillna(0.0)
    cash_factor = _daily_rf_factor(frame["rf"])

    # Position for today's return = yesterday's signal (shift by 1).
    holding = frame["target"].shift(1).fillna("CASH")

    equity = np.empty(len(frame))
    equity[0] = initial
    entry_trades = 0
    exit_trades = 0
    commission_paid = 0.0
    prev_hold = holding.iloc[0]

    tqqq_r = tqqq_ret.values
    cash_f = cash_factor.values
    hold_arr = holding.values

    for i in range(1, len(frame)):
        h = hold_arr[i]
        if h == "TQQQ":
            equity[i] = equity[i - 1] * (1.0 + tqqq_r[i])
        else:
            equity[i] = equity[i - 1] * cash_f[i]
        if h != prev_hold:
            # Rotating INTO TQQQ is an entry; rotating OUT to cash is an exit.
            if h == "TQQQ":
                entry_trades += 1
            else:
                exit_trades += 1
            commission_paid += commission
            equity[i] -= commission
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
    frame = md.frame.join(signals.frame[["target"]], how="inner")
    price = frame[asset]
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
    frame = md.frame.join(signals.frame[["target"]], how="inner")
    price = frame[asset].values
    idx = frame.index

    shares = 0.0
    invested = 0.0
    contrib_trades = 0
    equity = np.empty(len(frame))
    flow_dates: list[pd.Timestamp] = []
    flow_amounts: list[float] = []

    # Day-one lump sum, invested in the same asset.
    if initial_lump_sum > 0.0:
        shares += initial_lump_sum / price[0]
        invested += initial_lump_sum
        contrib_trades += 1
        flow_dates.append(idx[0])
        flow_amounts.append(-initial_lump_sum)

    for i in range(len(frame)):
        if i % every_n_days == 0:
            shares += contribution / price[i]
            invested += contribution
            contrib_trades += 1
            flow_dates.append(idx[i])
            flow_amounts.append(-contribution)
        equity[i] = shares * price[i]

    eq = pd.Series(equity, index=idx, name=label or asset)
    flow_dates.append(idx[-1])
    flow_amounts.append(float(eq.iloc[-1]))

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
    frame = md.frame.join(
        signals.frame[["target", "above", "overheated"]], how="inner"
    )
    tqqq_ret = frame["tqqq"].pct_change().fillna(0.0).values
    cash_factor = _daily_rf_factor(frame["rf"]).values

    # S&P 500 ballast sleeve. When no external SPY series is supplied we use the
    # cash/risk-free growth as a conservative stand-in so the sleeve is defined.
    if spy_proxy is not None:
        spy = spy_proxy.reindex(frame.index).ffill()
        spy_ret = spy.pct_change().fillna(0.0).values
    else:
        spy_ret = (cash_factor - 1.0)  # risk-free daily return

    above = frame["above"].shift(1).fillna(False).values
    overheated = frame["overheated"].shift(1).fillna(False).values

    # Three sleeves tracked in dollars.
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
        flow_amounts.append(-initial_lump_sum)
        if above[0] and not overheated[0]:
            tqqq_bal += initial_lump_sum
        elif above[0] and overheated[0]:
            spy_bal += initial_lump_sum
        else:
            cash_bal += initial_lump_sum

    n = len(frame)
    for i in range(n):
        # Grow each sleeve by its daily return first.
        if i > 0:
            tqqq_bal *= (1.0 + tqqq_ret[i])
            cash_bal *= cash_factor[i]
            spy_bal *= (1.0 + spy_ret[i])

        # Sell rule (signal EXIT): if below MA, move any TQQQ to cash.
        if not above[i] and tqqq_bal > 0.0:
            cash_bal += tqqq_bal
            tqqq_bal = 0.0
            exit_trades += 1
            cash_bal -= commission
            commission_paid += commission

        # Re-entry (signal ENTRY): if above MA and not overheated, move parked
        # cash into TQQQ. (Deposits parked in a downtrend get deployed on recross.)
        if above[i] and not overheated[i] and cash_bal > 0.0:
            tqqq_bal += cash_bal
            cash_bal = 0.0
            entry_trades += 1
            tqqq_bal -= commission
            commission_paid += commission

        # Portfolio value after market growth + rotations, but BEFORE today's
        # deposit. Comparing this to yesterday's close gives the pure investment
        # return, uncontaminated by the deposit inflow.
        pre_deposit = tqqq_bal + cash_bal + spy_bal
        if i > 0 and equity[i - 1] > 0.0:
            daily_ret[i] = pre_deposit / equity[i - 1] - 1.0

        # Deposit on schedule (a contribution trade, not a signal rotation).
        if i % every_n_days == 0:
            invested += contribution
            contrib_trades += 1
            flow_dates.append(idx[i])
            flow_amounts.append(-contribution)
            if above[i] and not overheated[i]:
                tqqq_bal += contribution
            elif above[i] and overheated[i]:
                spy_bal += contribution  # ballast
            else:
                cash_bal += contribution

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
