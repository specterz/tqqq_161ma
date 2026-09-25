"""Calibrate the synthetic-TQQQ financing spread against real TQQQ prices.

We have a synthetic TQQQ built from the Nasdaq-100 index (see ``data.py``):

    r_tqqq = L * r_index - (L - 1) * (rf + spread) / 252 - expense / 252

The ``spread`` (financing cost over the T-bill on the borrowed notional) is the
one soft assumption in that model. This script backs it out empirically:

1. Load **real** TQQQ daily closes (Yahoo ``chart`` JSON, split/див-adjusted).
2. Build synthetic TQQQ from the index over the overlapping dates.
3. Sweep the spread and pick the value that best matches real TQQQ, minimising
   the RMSE of daily-return differences (a level-independent fit).

It also reports the residual annualised drift at the best spread, i.e. how much
the model still over/under-shoots real TQQQ per year after calibration -- the
honest "how good is this proxy" number.

Usage::

    python src/calibrate.py                      # uses data/tqqq_raw.json
    python src/calibrate.py --json path.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from data import (
    DATA_DIR,
    FINANCING_SPREAD,
    TQQQ_EXPENSE_RATIO,
    TQQQ_LEVERAGE,
    TRADING_DAYS,
    _load_ndx,
    _load_riskfree,
)


def load_real_tqqq(json_path: Path) -> pd.Series:
    """Parse a Yahoo ``v8/finance/chart`` JSON into an adjusted-close series."""
    payload = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes = quote["close"]

    # Prefer adjusted close (handles the 2021 2:1 split) when present.
    adj = result["indicators"].get("adjclose")
    values = adj[0]["adjclose"] if adj else closes

    dates = pd.to_datetime(ts, unit="s").normalize()
    s = pd.Series(values, index=dates, name="tqqq_real").sort_index()
    s = s[~s.index.duplicated(keep="last")].dropna()
    return s


def synthetic_tqqq_returns(
    r_index: pd.Series,
    rf: pd.Series,
    spread: float,
    leverage: float = TQQQ_LEVERAGE,
    expense_ratio: float = TQQQ_EXPENSE_RATIO,
) -> pd.Series:
    """Daily synthetic TQQQ returns for a given financing spread."""
    daily_financing = (leverage - 1.0) * (rf + spread) / TRADING_DAYS
    daily_expense = expense_ratio / TRADING_DAYS
    return leverage * r_index - daily_financing - daily_expense


def calibrate(
    real: pd.Series,
    ndx: pd.Series,
    rf: pd.Series,
    spreads: np.ndarray,
) -> pd.DataFrame:
    """Sweep spreads, return a table of fit metrics per spread."""
    # Align everything on the real-TQQQ trading calendar.
    idx = real.index.intersection(ndx.index)
    real = real.reindex(idx)
    r_real = real.pct_change().dropna()

    r_index = ndx.reindex(idx).pct_change()
    rf_al = rf.reindex(idx).ffill().bfill()

    rows = []
    for spread in spreads:
        r_syn = synthetic_tqqq_returns(r_index, rf_al, spread).reindex(r_real.index)
        diff = (r_syn - r_real).dropna()
        rmse = float(np.sqrt(np.mean(diff.values**2)))
        # Residual annualised drift: mean daily return gap * 252.
        drift = float(diff.mean() * TRADING_DAYS)
        rows.append({"spread": spread, "rmse_daily": rmse, "resid_drift_ann": drift})
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Calibrate TQQQ financing spread")
    p.add_argument("--json", default=str(DATA_DIR / "TQQQ_real_yahoo.json"))
    p.add_argument("--lo", type=float, default=-0.02, help="min spread to test")
    p.add_argument("--hi", type=float, default=0.03, help="max spread to test")
    p.add_argument("--steps", type=int, default=101)
    return p.parse_args()


def main() -> None:
    args = parse_args()

    real = load_real_tqqq(Path(args.json))
    ndx = _load_ndx(DATA_DIR / "NDX.csv")
    rf = _load_riskfree(DATA_DIR / "tbill_dgs3mo.csv")

    overlap = real.index.intersection(ndx.index)
    print(
        f"Real TQQQ: {real.index.min().date()} -> {real.index.max().date()} "
        f"({len(real)} days)"
    )
    print(
        f"Overlap with index: {overlap.min().date()} -> {overlap.max().date()} "
        f"({len(overlap)} days)\n"
    )

    spreads = np.linspace(args.lo, args.hi, args.steps)
    table = calibrate(real, ndx, rf, spreads)

    best = table.loc[table["rmse_daily"].idxmin()]
    zero_drift = table.loc[table["resid_drift_ann"].abs().idxmin()]

    print("Best fit by daily-return RMSE:")
    print(
        f"  spread = {best['spread'] * 100:+.2f}%  "
        f"rmse_daily = {best['rmse_daily'] * 1e4:.2f} bps  "
        f"residual drift = {best['resid_drift_ann'] * 100:+.2f}%/yr"
    )
    print("\nSpread that zeroes annual drift (level match):")
    print(
        f"  spread = {zero_drift['spread'] * 100:+.2f}%  "
        f"rmse_daily = {zero_drift['rmse_daily'] * 1e4:.2f} bps  "
        f"residual drift = {zero_drift['resid_drift_ann'] * 100:+.2f}%/yr"
    )

    # Cumulative level comparison at the best-RMSE spread.
    idx = real.index.intersection(ndx.index)
    r_real = real.reindex(idx).pct_change().fillna(0.0)
    r_index = ndx.reindex(idx).pct_change().fillna(0.0)
    rf_al = rf.reindex(idx).ffill().bfill()
    r_syn = synthetic_tqqq_returns(r_index, rf_al, float(best["spread"])).fillna(0.0)

    real_level = (1.0 + r_real).cumprod()
    syn_level = (1.0 + r_syn).cumprod()
    end_gap = float(syn_level.iloc[-1] / real_level.iloc[-1] - 1.0)
    print(
        f"\nCumulative level over {len(idx)} days at best spread: "
        f"synthetic ends {end_gap * 100:+.1f}% vs real TQQQ"
    )
    print(
        f"(current default spread in data.py is {FINANCING_SPREAD * 100:+.2f}%; "
        f"calibrated best is {best['spread'] * 100:+.2f}%)"
    )


if __name__ == "__main__":
    main()
