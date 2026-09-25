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
    p.add_argument("--threshold", type=float, default=OVERHEATED_THRESHOLD)
    p.add_argument(
        "--no-overheating",
        action="store_true",
        help="disable the +5%% overheated/ballast rule (contributions above the "
             "MA always buy TQQQ; no S&P ballast sleeve)",
    )
    p.add_argument(
        "--financing-spread",
        type=float,
        default=None,
        help="TQQQ financing spread over T-bill (default: calibrated -0.40%%)",
    )
    p.add_argument(
        "--expense-ratio",
        type=float,
        default=None,
        help="TQQQ annual expense ratio (default: 0.95%%)",
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

    md_kwargs = {"auto_update": not args.no_update, "max_age_days": args.max_age_days}
    if args.financing_spread is not None:
        md_kwargs["financing_spread"] = args.financing_spread
    if args.expense_ratio is not None:
        md_kwargs["expense_ratio"] = args.expense_ratio
    md_full = build_market_data(**md_kwargs).slice(args.start, args.end)

    ma_windows = sorted(set(args.ma))

    # Align every series (all strategies AND benchmarks) to a common start: the
    # first date on which the LONGEST MA tested is defined. Signals are computed
    # on the full data, then everything is sliced to this common date so the
    # comparison window is identical no matter which MAs are swept. Without this,
    # a longer MA drops more warmup rows and benchmarks would silently start on
    # different dates for different sweeps.
    warmup = max(ma_windows)
    if len(md_full.frame) <= warmup:
        raise SystemExit(f"Not enough data ({len(md_full.frame)} days) for a "
                         f"{warmup}-day MA warmup.")
    common_start = md_full.index[warmup - 1]
    md = md_full.slice(common_start.strftime("%Y-%m-%d"))

    # None disables the overheated/ballast rule entirely.
    threshold = None if args.no_overheating else args.threshold

    print(
        f"Data: {md.index.min().date()} -> {md.index.max().date()} "
        f"({len(md.frame)} trading days, aligned to {warmup}-day warmup)"
    )
    print(
        f"MA windows: {', '.join(str(m) for m in ma_windows)}  "
        + ("overheated: DISABLED" if threshold is None
           else f"overheated: +{threshold * 100:.0f}%")
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

    # One strategy result per MA window. Signals are computed on the full series
    # (so each MA is fully warmed up) then sliced to the common start.
    strat_results = []
    for ma in ma_windows:
        # Compute on md_full (the UN-sliced series) so the MA sees all prior
        # history and is genuinely warm at common_start. Computing on the
        # already-sliced series would start each MA cold instead.
        signals_full = compute_signals(
            md_full.frame["qqq"], ma_window=ma, overheated_threshold=threshold
        )
        # Now trim the warmed signal down to the shared comparison window.
        signals = Signals(signals_full.frame[signals_full.index >= common_start])
        if args.mode == "lump_sum":
            strat = run_lump_sum(
                md, signals, initial=args.initial,
                commission=args.commission, label=f"{ma}MA Strategy",
            )
        else:
            strat = run_contributions(
                md, signals, contribution=args.contribution,
                every_n_days=args.every, commission=args.commission or 2.0,
                initial_lump_sum=args.initial_lump_sum, label=f"{ma}MA Strategy",
            )
        strat_results.append(strat)

    # Benchmarks are MA-independent; build once over the common-start window.
    bench_signals = compute_signals(
        md_full.frame["qqq"], ma_window=warmup, overheated_threshold=threshold
    )
    bench_signals = Signals(bench_signals.frame[bench_signals.index >= common_start])
    if args.mode == "lump_sum":
        benchmarks = [
            run_hold(md, bench_signals, "tqqq", initial=args.initial, label="TQQQ Hold"),
            run_hold(md, bench_signals, "qqq", initial=args.initial, label="QQQ Hold"),
        ]
    else:
        benchmarks = [
            run_hold_dca(
                md, bench_signals, "tqqq", contribution=args.contribution,
                every_n_days=args.every, initial_lump_sum=args.initial_lump_sum,
                label="TQQQ DCA",
            ),
            run_hold_dca(
                md, bench_signals, "qqq", contribution=args.contribution,
                every_n_days=args.every, initial_lump_sum=args.initial_lump_sum,
                label="QQQ DCA",
            ),
        ]

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
        print(f"\nBest MA by {best.annualized_kind}: {best.label} "
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
    qqq_full = md_full.frame["qqq"]
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
