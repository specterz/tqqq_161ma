"""JSON bridge between the browser (Pyodide) and the real backtest engine.

The website calls :func:`run` with a plain config dict and gets back a
JSON-serialisable dict of results — the SAME numbers the CLI prints, because it
reuses the exact same ``build_market_data`` / ``compute_signals`` / ``run_*``
functions and mirrors ``run.py``'s orchestration (MA sweep, leverage sweep,
overheating band, common-start alignment, best-to-worst sort).

Design notes for the browser:
* ``auto_update`` is forced OFF here — the browser can't fetch via urllib, and
  the CSVs are pre-mounted into Pyodide's filesystem by app.js. Data freshness
  is handled outside (the site ships a recent data snapshot).
* Equity curves are downsampled for transport (charting doesn't need every one
  of ~10k daily points); metrics are always computed on the FULL series.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from data import build_market_data
from strategy import Signals, compute_signals
from backtest import (
    run_contributions,
    run_hold,
    run_hold_dca,
    run_lump_sum,
)

_ETF = {1.0: "QQQ", 2.0: "QLD", 3.0: "TQQQ"}


def _etf_of(lev: float) -> str:
    return _ETF.get(float(lev), f"{lev:g}x")


def _downsample(series: pd.Series, max_points: int = 800) -> dict[str, list]:
    """Thin an equity/price series to <= max_points for charting (keep ends)."""
    n = len(series)
    if n <= max_points:
        idx = range(n)
    else:
        step = n / max_points
        idx = sorted({int(i * step) for i in range(max_points)} | {n - 1})
    return {
        "dates": [series.index[i].strftime("%Y-%m-%d") for i in idx],
        "values": [round(float(series.iloc[i]), 2) for i in idx],
    }


def _result_dict(r: Any, *, with_equity: bool = False) -> dict[str, Any]:
    """Convert a BacktestResult into a JSON-safe dict."""
    def clean(x: float) -> float | None:
        # JSON can't carry NaN/inf; send null so the UI shows "n/a".
        return None if x != x or x in (float("inf"), float("-inf")) else round(float(x), 6)

    d = {
        "label": r.label,
        "invested": round(float(r.invested), 2),
        "finalValue": round(float(r.final_value), 2),
        "totalReturn": clean(r.total_return),
        "annualized": clean(r.annualized),
        "annualizedKind": r.annualized_kind,
        "sharpe": clean(r.sharpe),
        "maxDrawdown": clean(r.max_drawdown),
        "entries": int(r.entry_trades),
        "exits": int(r.exit_trades),
        "contribs": int(r.contrib_trades),
        "commissionPaid": round(float(r.commission_paid), 2),
    }
    if with_equity:
        d["equity"] = _downsample(r.equity)
    return d


def _monthly_table(r: Any) -> dict[str, Any]:
    """Year x month return grid (percent) + Sum/Avg footer, as plain data."""
    m = r.monthly_returns()
    if m.empty:
        return {"years": [], "rows": [], "sum": [], "avg": [], "ytd": []}

    years = sorted({y for (y, _mo) in m.index})
    col_sums = [0.0] * 12
    col_counts = [0] * 12
    ytd_sum = 0.0
    ytd_count = 0
    rows = []
    for y in years:
        month_vals = []
        cells = []
        for mo in range(1, 13):
            if (y, mo) in m.index:
                v = float(m.loc[(y, mo)])
                month_vals.append(v)
                col_sums[mo - 1] += v
                col_counts[mo - 1] += 1
                cells.append(round(v * 100, 1))
            else:
                cells.append(None)
        ytd = 1.0
        for v in month_vals:
            ytd *= 1.0 + v
        ytd = ytd - 1.0 if month_vals else None
        if ytd is not None:
            ytd_sum += ytd
            ytd_count += 1
        rows.append({"year": y, "months": cells,
                     "ytd": round(ytd * 100, 1) if ytd is not None else None})

    sum_row = [round(col_sums[i] * 100, 1) for i in range(12)]
    avg_row = [round(col_sums[i] / col_counts[i] * 100, 1) if col_counts[i] else None
               for i in range(12)]
    return {
        "label": r.label,
        "rows": rows,
        "sum": sum_row,
        "avg": avg_row,
        "sumYtd": round(ytd_sum * 100, 1),
        "avgYtd": round(ytd_sum / ytd_count * 100, 1) if ytd_count else None,
    }


def run(config: dict[str, Any]) -> dict[str, Any]:
    """Run the backtest for a config dict; return JSON-serialisable results.

    Config keys (all optional except mode):
      mode            "lump_sum" | "contributions"
      ma              list[int] of MA windows (default [161])
      leverage        list[float] of leverage factors (default [3.0])
      overheating     float band in PERCENT, or None to disable (default None)
      start, end      ISO date strings or None
      initial         lump-sum starting capital (default 10000)
      contribution    per-deposit $ (contributions mode; default 100)
      every           trading days between deposits (default 21)
      initialLumpSum  day-one deposit in contributions mode (default 0)
      commission      per-trade $ (default 0; contributions uses 2 if 0)
      financingSpread decimal (e.g. -0.004) or None -> calibrated default
      expenseRatio    decimal (e.g. 0.0095) or None -> default
      wantMonthly     bool -> include the MoM table for the best strategy
    """
    mode = config.get("mode", "lump_sum")
    ma_windows = sorted(set(int(x) for x in config.get("ma") or [161]))
    lev_in = config.get("leverage") or [3.0]
    seen: set = set()
    leverages = [float(x) for x in lev_in if not (float(x) in seen or seen.add(float(x)))]
    multi_lev = len(leverages) > 1
    oh = config.get("overheating")
    threshold = None if oh is None else float(oh) / 100.0

    start = config.get("start") or None
    end = config.get("end") or None
    initial = float(config.get("initial", 10_000))
    contribution = float(config.get("contribution", 100))
    every = int(config.get("every", 21))
    initial_lump_sum = float(config.get("initialLumpSum", 0))
    commission = float(config.get("commission", 0))
    want_monthly = bool(config.get("wantMonthly", False))

    base_kwargs: dict[str, Any] = {"auto_update": False}  # browser: never fetch
    if config.get("financingSpread") is not None:
        base_kwargs["financing_spread"] = float(config["financingSpread"])
    if config.get("expenseRatio") is not None:
        base_kwargs["expense_ratio"] = float(config["expenseRatio"])

    # Build + cache market data per leverage (data read once; only tqqq differs).
    md_full_by_lev = {}
    for lev in leverages:
        md_full_by_lev[lev] = build_market_data(leverage=lev, **base_kwargs).slice(start, end)
    md_full0 = md_full_by_lev[leverages[0]]

    warmup = max(ma_windows)
    if len(md_full0.frame) <= warmup:
        return {"error": f"Not enough data ({len(md_full0.frame)} days) for a "
                         f"{warmup}-day MA warmup. Widen the date range."}
    common_start = md_full0.index[warmup - 1]
    cs = common_start.strftime("%Y-%m-%d")

    signals_by_ma = {}
    for ma in ma_windows:
        sf = compute_signals(md_full0.frame["qqq"], ma_window=ma,
                             overheated_threshold=threshold)
        signals_by_ma[ma] = Signals(sf.frame[sf.frame.index >= common_start])
    bench_sf = compute_signals(md_full0.frame["qqq"], ma_window=warmup,
                               overheated_threshold=threshold)
    bench_signals = Signals(bench_sf.frame[bench_sf.frame.index >= common_start])
    md0 = md_full0.slice(cs)

    def label_of(lev: float, ma: int) -> str:
        return f"{_etf_of(lev)} {ma}MA" if multi_lev else f"{ma}MA Strategy"

    strat_results = []
    benchmarks = []
    for lev in leverages:
        md_lev = md_full_by_lev[lev].slice(cs)
        etf = _etf_of(lev)
        for ma in ma_windows:
            sig = signals_by_ma[ma]
            if mode == "lump_sum":
                strat_results.append(run_lump_sum(
                    md_lev, sig, initial=initial, commission=commission,
                    label=label_of(lev, ma)))
            else:
                strat_results.append(run_contributions(
                    md_lev, sig, contribution=contribution, every_n_days=every,
                    commission=commission or 2.0, initial_lump_sum=initial_lump_sum,
                    label=label_of(lev, ma)))
        if mode == "lump_sum":
            benchmarks.append(run_hold(md_lev, bench_signals, "tqqq",
                                       initial=initial, label=f"{etf} Hold"))
        else:
            benchmarks.append(run_hold_dca(
                md_lev, bench_signals, "tqqq", contribution=contribution,
                every_n_days=every, initial_lump_sum=initial_lump_sum,
                label=f"{etf} DCA"))

    if mode == "lump_sum":
        benchmarks.append(run_hold(md0, bench_signals, "qqq", initial=initial,
                                   label="QQQ Hold"))
    else:
        benchmarks.append(run_hold_dca(
            md0, bench_signals, "qqq", contribution=contribution, every_n_days=every,
            initial_lump_sum=initial_lump_sum, label="QQQ DCA"))

    all_results = strat_results + benchmarks
    all_results.sort(
        key=lambda r: (r.annualized if r.annualized == r.annualized else float("-inf")),
        reverse=True,
    )

    best = max(strat_results, key=lambda r: (
        r.annualized if r.annualized == r.annualized else float("-inf")))

    # Signal panel: QQQ + each MA over the window (MA computed on full history).
    qqq_full = md_full0.frame["qqq"]
    qqq_win = md0.frame["qqq"]
    signal_panel = {
        "qqq": _downsample(qqq_win),
        "mas": [],
    }
    for ma in ma_windows:
        ma_line = qqq_full.rolling(ma, min_periods=ma).mean()
        ma_line = ma_line[ma_line.index >= common_start]
        signal_panel["mas"].append({"window": ma, "line": _downsample(ma_line)})

    out: dict[str, Any] = {
        "meta": {
            "start": md0.index.min().strftime("%Y-%m-%d"),
            "end": md0.index.max().strftime("%Y-%m-%d"),
            "tradingDays": int(len(md0.frame)),
            "warmup": warmup,
            "leverages": [f"{x:g}x ({_etf_of(x)})" for x in leverages],
            "maWindows": ma_windows,
            "mode": mode,
            "overheating": None if threshold is None else round(threshold * 100, 1),
            "annualizedKind": best.annualized_kind,
            "bestLabel": best.label,
        },
        "results": [_result_dict(r, with_equity=True) for r in all_results],
        "signalPanel": signal_panel,
    }
    if want_monthly:
        out["monthly"] = _monthly_table(best)
    return out


def run_json(config_json: str) -> str:
    """Convenience: accept a JSON string, return a JSON string (for Pyodide)."""
    import json
    return json.dumps(run(json.loads(config_json)))
