"""CLI entry point: run the AGITQ 161-day MA backtest and plot results.

Examples
--------
Lump-sum over the full history::

    python src/run.py --mode lump_sum --start 2011-01-01

Contribution (DCA) mode with the ballast rule, matching the app-style test::

    python src/run.py --mode contributions --contribution 100 --every 21
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: write PNG, never open a window
import matplotlib.pyplot as plt

from data import build_market_data
from strategy import MA_WINDOW, OVERHEATED_THRESHOLD, Signals, compute_signals
from backtest import (
    format_monthly_table,
    format_results_table,
    run_contributions,
    run_hold,
    run_hold_dca,
    run_lump_sum,
)

OUT_DIR = Path(__file__).resolve().parent.parent / "results"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AGITQ 161-day MA TQQQ backtest")
    p.add_argument("--mode", choices=["lump_sum", "contributions"], default="lump_sum")
    p.add_argument("--start", default=None, help="start date YYYY-MM-DD")
    p.add_argument("--end", default=None, help="end date YYYY-MM-DD")
    p.add_argument("--initial", type=float, default=10_000.0)
    p.add_argument("--contribution", type=float, default=100.0)
    p.add_argument(
        "--initial-lump-sum",
        type=float,
        default=0.0,
        help="one-off amount invested on day one (contributions mode)",
    )
    p.add_argument("--every", type=int, default=21, help="trading days between deposits")
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument(
        "--ma",
        type=int,
        nargs="+",
        default=[MA_WINDOW],
        help="one or more MA windows to test, e.g. --ma 100 150 161 200 250",
    )
    p.add_argument(
        "--overheating",
        type=float,
        default=None,
        metavar="X",
        help="enable the overheated rule with a +X%% band: contribute (buy TQQQ) "
             "only while price is above the MA AND below MA+X%%; above MA+X%% the "
             "deposit goes into the VOO/S&P sleeve instead. Omitted = rule OFF "
             "(default): any deposit above the MA buys TQQQ.",
    )
    p.add_argument(
        "--leverage",
        type=float,
        nargs="+",
        default=None,
        metavar="L",
        help="one or more leveraged-ETF factors to model: 3 = TQQQ (default), "
             "2 = QLD, 1 = QQQ. Pass several to compare, e.g. --leverage 2 3. "
             "Note the financing spread is calibrated for 3x TQQQ; other factors "
             "are directionally modelled, not calibrated.",
    )
    p.add_argument(
        "--financing-spread",
        type=float,
        default=None,
        help="financing spread over T-bill (default: calibrated -0.40%% for 3x)",
    )
    p.add_argument(
        "--expense-ratio",
        type=float,
        default=None,
        help="leveraged-ETF annual expense ratio (default: 0.95%%)",
    )
    p.add_argument(
        "--mom",
        action="store_true",
        help="print a month-over-month return table (year x month grid) for the "
             "best strategy, in the C2 monthly-returns format",
    )
    p.add_argument("--no-plot", action="store_true")
    p.add_argument(
        "--format",
        choices=["svg", "png", "pdf"],
        default="svg",
        help="chart file format (default svg: vector, infinite zoom)",
    )
    p.add_argument(
        "--dpi",
        type=int,
        default=200,
        help="raster resolution in DPI (only used for --format png; default 200)",
    )
    p.add_argument(
        "--no-update",
        action="store_true",
        help="skip the auto-download/refresh of NDX data (offline mode)",
    )
    p.add_argument(
        "--max-age-days",
        type=float,
        default=None,
        help="calendar-day staleness rule for NDX data (default: close-aware, "
             "refresh once the latest session's close has passed); 0 = always",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    ma_windows = sorted(set(args.ma))
    leverages = args.leverage if args.leverage is not None else [3.0]
    # De-dupe while preserving the given order (so labels read left-to-right).
    seen = set()
    leverages = [x for x in leverages if not (x in seen or seen.add(x))]
    multi_lev = len(leverages) > 1

    # Overheated rule is OFF by default; --overheating X (percent) turns it on.
    threshold = None if args.overheating is None else args.overheating / 100.0

    def etf_of(lev: float) -> str:
        return {1.0: "QQQ", 2.0: "QLD", 3.0: "TQQQ"}.get(lev, f"{lev:g}x")

    # Base kwargs shared across every leverage build (data source + tunables).
    base_kwargs = {"auto_update": not args.no_update, "max_age_days": args.max_age_days}
    if args.financing_spread is not None:
        base_kwargs["financing_spread"] = args.financing_spread
    if args.expense_ratio is not None:
        base_kwargs["expense_ratio"] = args.expense_ratio

    # Build the market data for each leverage ONCE and cache it. The data files
    # are refreshed only on the FIRST build (honouring --no-update/--max-age);
    # subsequent builds pass auto_update=False so we never re-check or re-fetch
    # the same NDX/SPX data — only the leveraged 'tqqq' column is recomputed.
    md_full_by_lev = {}
    for i, lev in enumerate(leverages):
        kw = dict(base_kwargs)
        if i > 0:
            kw["auto_update"] = False  # data already fresh from the first build
        md_full_by_lev[lev] = build_market_data(leverage=lev, **kw).slice(
            args.start, args.end
        )
    md_full0 = md_full_by_lev[leverages[0]]  # QQQ/signals source (leverage-independent)
    warmup = max(ma_windows)
    if len(md_full0.frame) <= warmup:
        raise SystemExit(f"Not enough data ({len(md_full0.frame)} days) for a "
                         f"{warmup}-day MA warmup.")
    common_start = md_full0.index[warmup - 1]

    # Pre-compute the warmed signal per MA once (depends only on QQQ).
    signals_by_ma = {}
    for ma in ma_windows:
        sf = compute_signals(
            md_full0.frame["qqq"], ma_window=ma, overheated_threshold=threshold
        )
        signals_by_ma[ma] = Signals(sf.frame[sf.frame.index >= common_start])
    bench_sf = compute_signals(
        md_full0.frame["qqq"], ma_window=warmup, overheated_threshold=threshold
    )
    bench_signals = Signals(bench_sf.frame[bench_sf.frame.index >= common_start])

    md0 = md_full0.slice(common_start.strftime("%Y-%m-%d"))

    print(
        f"Data: {md0.index.min().date()} -> {md0.index.max().date()} "
        f"({len(md0.frame)} trading days, aligned to {warmup}-day warmup)"
    )
    print("Leverage: " + ", ".join(f"{x:g}x ({etf_of(x)})" for x in leverages))
    print(
        f"MA windows: {', '.join(str(m) for m in ma_windows)}  "
        + ("overheated: OFF" if threshold is None
           else f"overheated: contribute only below MA+{threshold * 100:.1f}%")
    )
    if args.mode == "contributions":
        print(
            f"Deposits: ${args.contribution:,.0f} every {args.every} trading days"
            + (
                f" + ${args.initial_lump_sum:,.0f} initial lump sum"
                if args.initial_lump_sum > 0
                else ""
            )
        )
    print()

    # Prefix strategy labels with the ETF only when comparing multiple leverages,
    # so a single-leverage run keeps its familiar "161MA Strategy" label.
    def strat_label(lev: float, ma: int) -> str:
        return f"{etf_of(lev)} {ma}MA" if multi_lev else f"{ma}MA Strategy"

    strat_results = []
    benchmarks = []
    md = md0  # for the chart panel (leverage-independent QQQ/MA)
    for li, lev in enumerate(leverages):
        # Reuse the cached full series for this leverage (built + refreshed once
        # above); just slice it to the shared comparison window.
        md_lev = md_full_by_lev[lev].slice(common_start.strftime("%Y-%m-%d"))
        etf = etf_of(lev)

        for ma in ma_windows:
            signals = signals_by_ma[ma]
            if args.mode == "lump_sum":
                strat = run_lump_sum(
                    md_lev, signals, initial=args.initial,
                    commission=args.commission, label=strat_label(lev, ma),
                )
            else:
                strat = run_contributions(
                    md_lev, signals, contribution=args.contribution,
                    every_n_days=args.every, commission=args.commission or 2.0,
                    initial_lump_sum=args.initial_lump_sum, label=strat_label(lev, ma),
                )
            strat_results.append(strat)

        # One leveraged-hold benchmark per leverage.
        if args.mode == "lump_sum":
            benchmarks.append(
                run_hold(md_lev, bench_signals, "tqqq", initial=args.initial,
                         label=f"{etf} Hold")
            )
        else:
            benchmarks.append(
                run_hold_dca(md_lev, bench_signals, "tqqq", contribution=args.contribution,
                             every_n_days=args.every, initial_lump_sum=args.initial_lump_sum,
                             label=f"{etf} DCA")
            )

    # QQQ (1x) benchmark once, at the end (leverage-independent).
    if args.mode == "lump_sum":
        benchmarks.append(run_hold(md0, bench_signals, "qqq", initial=args.initial, label="QQQ Hold"))
    else:
        benchmarks.append(
            run_hold_dca(md0, bench_signals, "qqq", contribution=args.contribution,
                         every_n_days=args.every, initial_lump_sum=args.initial_lump_sum,
                         label="QQQ DCA")
        )

    results = strat_results + benchmarks
    # Sort best-to-worst by annualized return (CAGR for lump-sum, IRR for
    # contributions). NaN annualized (degenerate fits) sorts last.
    results = sorted(
        results,
        key=lambda r: (r.annualized if r.annualized == r.annualized else float("-inf")),
        reverse=True,
    )
    print(format_results_table(results))

    # Best strategy by annualized return (used for --mom and the summary line).
    best = max(strat_results, key=lambda r: r.annualized)
    if len(strat_results) > 1:
        print(f"\nBest strategy by {best.annualized_kind}: {best.label} "
              f"({best.annualized * 100:+.1f}%)")

    if args.mom:
        print()
        print(format_monthly_table(best))

    if args.no_plot:
        return

    OUT_DIR.mkdir(exist_ok=True)
    fig, ax = plt.subplots(2, 1, figsize=(14, 11), height_ratios=[2, 1])

    for r in strat_results:
        ax[0].plot(r.equity.index, r.equity.values, label=r.label, linewidth=1.4)
    for r in benchmarks:
        ax[0].plot(r.equity.index, r.equity.values, label=r.label,
                   linewidth=1.2, linestyle="--", alpha=0.7)
    ax[0].set_yscale("log")
    ma_title = "/".join(str(m) for m in ma_windows)
    ax[0].set_title(
        f"AGITQ {ma_title}-day MA strategy vs buy-and-hold ({args.mode})"
    )
    ax[0].set_ylabel("Equity ($, log scale)")
    ax[0].legend(loc="upper left")
    ax[0].grid(True, which="both", alpha=0.3)

    # Signal panel: QQQ vs each MA window. MAs are computed on the full series
    # (so they're warmed up) then sliced to the displayed window.
    qqq_full = md_full0.frame["qqq"]
    qqq = md.frame["qqq"]
    ax[1].plot(qqq.index, qqq.values, label="QQQ", color="#1f77b4", linewidth=1.0)
    for ma in ma_windows:
        ma_line = qqq_full.rolling(ma, min_periods=ma).mean()
        ma_line = ma_line[ma_line.index >= common_start]
        ax[1].plot(ma_line.index, ma_line.values, label=f"{ma}-day MA", linewidth=1.0)
    ax[1].set_yscale("log")
    ax[1].set_ylabel("QQQ proxy (log)")
    ax[1].legend(loc="upper left")
    ax[1].grid(True, which="both", alpha=0.3)

    fig.tight_layout()
    out_path = OUT_DIR / f"backtest_{args.mode}.{args.format}"
    if args.format == "png":
        fig.savefig(out_path, dpi=args.dpi)
        px_w, px_h = int(14 * args.dpi), int(11 * args.dpi)
        detail = f"{px_w}x{px_h}px @ {args.dpi} DPI"
    else:
        # SVG/PDF are vector: resolution-independent, zoom to any level.
        fig.savefig(out_path)
        detail = "vector, infinite zoom"
    print(f"\nChart saved -> {out_path} ({detail})")


if __name__ == "__main__":
    main()
