# TQQQ_161ma — AGITQ 161-day MA Strategy

A rules-based TQQQ trend-following strategy and backtest, based on the r/TQQQ
post ["The ultimate TQQQ strategy"](https://www.reddit.com/r/TQQQ/comments/1q886nf/the_ultimate_tqqq_strategy/).
The approach is popular in Korean investing communities and is attributed to
"AGITQ".

## The rules

The signal is driven by **QQQ** (the Nasdaq-100), not by TQQQ itself:

1. **Buy TQQQ** when QQQ closes **above** its **161-day** simple moving average.
2. **Sell to treasury (SGOV / cash)** when QQQ closes **below** the 161-day MA.
3. **Overheated / ballast rule:** when QQQ is more than **+5%** above its
   161-day MA, do not add new money to TQQQ. New contributions go into an
   **S&P 500** sleeve instead. This ballast cushions drawdowns.
4. Check once per day and follow the rules — no discretion.

### Why 161 days?

The original idea used TQQQ's own 200-day MA. Backtests suggested that using
**QQQ's 161-day MA** cuts down on whipsaw (false) signals while keeping similar
returns — fewer round-trips, less stress.

## How this project models the data

The workspace only ships a long Nasdaq-100 index history, so QQQ and TQQQ are
built synthetically (see `src/data.py`):

- **QQQ** — the Nasdaq-100 index normalised to a \$100 start (tracks 1:1).
- **TQQQ** — a synthetic **3x daily-reset** ETF:

  ```
  r_tqqq = 3 * r_index - 2 * (rf + spread) / 252 - 0.95% / 252
  ```

  i.e. 3x the daily index return, minus financing on the 2x borrowed notional
  (3-month T-bill + a financing `spread`) and minus the 0.95% annual expense
  ratio. This is the standard leveraged-ETF path model and captures volatility
  drag. The `spread` is **calibrated against real TQQQ** — see below.

- **Risk-free / SGOV sleeve** — grows at the daily 3-month T-bill rate
  (`data/tbill_dgs3mo.csv`, FRED DGS3MO).

> These are **synthetic** series for research, not real TQQQ/SGOV prices. Results
> will differ from live products, especially before TQQQ existed (2010).

### Auto-updating the NDX data

`build_market_data` refreshes `data/NDX.csv` on demand via `src/fetch_data.py`:

- **Missing file** → downloads the Nasdaq-100 (`^NDX`) history from Yahoo
  Finance's public chart API.
- **Stale file** → fetches the latest and **merges** it with the cached CSV, so
  the long bundled history is preserved while the recent tail is extended.
  Staleness is **close-aware** by default: it refetches once the most recent
  trading session's US market close (16:00 ET + 30 min) has passed and that
  bar is missing. So it *won't* refetch pointlessly intraday or on a weekend,
  but *will* pick up today's bar the moment it's available. Pass
  `--max-age-days N` to switch to a plain calendar-day rule instead (`0` =
  always refetch).
- **Fresh file** → no network call.
- **Network failure** with a cached file present → falls back to the cache
  (only a *missing* file with no network is fatal).

Every run prints a one-line status so you always know whether data changed:

```
[fetch_data] data already up to date (latest 2026-09-22); no fetch needed.
[fetch_data] updated: +2 new row(s), latest 2026-09-20 -> 2026-09-22.
[fetch_data] fetch succeeded but no new rows (latest still 2026-09-22).
[fetch_data] auto-update off; using cached data (latest 2026-09-22).
[fetch_data] update failed (<error>); using cached data (latest 2026-09-22).
```

This happens automatically on every `run.py` invocation. To control it:

```bash
# Force offline / reproducible runs (never touch the network)
python src/run.py --mode lump_sum --start 2011-01-01 --no-update

# Consider data fresh for a week before re-downloading
python src/run.py --mode lump_sum --max-age-days 7

# Manually refresh the CSV outside a backtest
python src/fetch_data.py            # refresh if stale
python src/fetch_data.py --force    # always re-download
```

The downloaded CSV keeps the exact `"Date","Price"` format the loader expects,
so the auto-updated file and the original bundled file are interchangeable.

### Calibrating the financing spread

`src/calibrate.py` backs the spread out empirically instead of guessing it. It
loads **real, split-adjusted TQQQ** daily closes (Yahoo chart JSON, bundled at
`data/TQQQ_real_yahoo.json`), rebuilds synthetic TQQQ from the index over the
overlapping 2010→2026 window, and sweeps the spread to find the value that
minimises the RMSE of daily-return differences.

Result over 4,177 overlapping trading days:

- **Best-fit spread ≈ −0.40%** (a *negative* spread — real TQQQ tracked 3x the
  index slightly better than a plain T-bill borrow cost implies, thanks to
  securities-lending income and favourable swap terms).
- Residual annual drift at that spread is ~0.0%/yr, and daily-return RMSE is
  ~22 bps.
- A ~−13.5% *cumulative* level gap remains over 16 years even at the best
  spread: a single constant can't capture daily-reset path effects and the
  price-vs-total-return index distinction. This is the honest limit of a
  1-parameter proxy.

The calibrated **−0.40%** is now the default in `data.py`. Re-run the
calibration any time with:

```bash
python src/calibrate.py
```

To stress-test other assumptions without editing code, both the spread and the
expense ratio are CLI flags on `run.py`:

```bash
# Conservative: original +0.50% borrow assumption
python src/run.py --mode lump_sum --start 2011-01-01 --financing-spread 0.005

# Cheaper fund assumption
python src/run.py --mode lump_sum --start 2011-01-01 --expense-ratio 0.0075
```

## Layout

```
TQQQ_161ma/
├── data/
│   ├── NDX.csv              # Nasdaq-100 daily index (1985-2026)
│   └── tbill_dgs3mo.csv     # 3-month T-bill rate (FRED DGS3MO)
├── src/
│   ├── data.py              # load data, build QQQ / synthetic TQQQ / rf
│   ├── strategy.py          # 161-day MA signal + overheated rule
│   ├── backtest.py          # lump-sum & contribution engines + metrics
│   ├── calibrate.py         # fit the financing spread to real TQQQ
│   └── run.py               # CLI + chart output
├── data/TQQQ_real_yahoo.json  # real TQQQ closes (for calibration)
└── results/                 # generated charts
```

## Usage

```bash
pip install -r requirements.txt

# Lump-sum backtest from 2011 (approx TQQQ era)
python src/run.py --mode lump_sum --start 2011-01-01

# Dollar-cost-averaging with the overheated ballast rule
python src/run.py --mode contributions --contribution 100 --every 21 --start 2020-01-01

# DCA plus a one-off lump sum invested on day one
python src/run.py --mode contributions --contribution 100 --every 7 --initial-lump-sum 10000 --start 2004-01-01

# Sweep several MA windows in one run (comparison table + overlay chart)
python src/run.py --mode lump_sum --start 2011-01-01 --ma 100 150 161 200 250
```

### Sweeping multiple MA windows

`--ma` accepts one or more windows. Pass several and the run tests each in a
single pass, prints an **aligned comparison table sorted best-to-worst by
annualized return** (CAGR for lump-sum, IRR for contributions — strategies and
benchmarks interleaved by rank), flags the best window, and overlays every
strategy curve (plus the benchmarks) on one chart. The terminal table looks
like:

```
+----------------+----------+------------+-----------+--------+--------+--------+--------+------+
| Strategy       | Invested |      Final |     Total |   CAGR | Sharpe |  MaxDD | Trades | Comm |
+----------------+----------+------------+-----------+--------+--------+--------+--------+------+
| TQQQ Hold      |  $10,000 | $2,153,532 | +21435.3% | +44.0% |   0.88 | -81.7% |      1 |   $0 |
| 161MA Strategy |  $10,000 | $1,702,474 | +16924.7% | +41.7% |   0.95 | -55.0% |     85 |   $0 |
| 200MA Strategy |  $10,000 | $1,284,556 | +12745.6% | +39.0% |   0.90 | -55.0% |     69 |   $0 |
| 150MA Strategy |  $10,000 | $1,027,518 | +10175.2% | +36.9% |   0.88 | -59.6% |    103 |   $0 |
| 250MA Strategy |  $10,000 |   $904,003 |  +8940.0% | +35.8% |   0.84 | -61.1% |     79 |   $0 |
| 100MA Strategy |  $10,000 |   $241,935 |  +2319.4% | +24.1% |   0.68 | -58.1% |    157 |   $0 |
| QQQ Hold       |  $10,000 |   $135,559 |  +1255.6% | +19.4% |   0.88 | -35.6% |      1 |   $0 |
+----------------+----------+------------+-----------+--------+--------+--------+--------+------+
```

The header adapts to the metric (CAGR vs IRR) and the columns auto-size.

**Sharpe ratio** is the annualized risk-adjusted return: mean daily excess
return (over the T-bill) divided by daily volatility, times √252. Higher is
better per unit of risk. Note that here **161MA has the best Sharpe of the whole
table (0.95) — beating buy-and-hold TQQQ (0.88)**: it captures most of TQQQ's
return with far less volatility, which is the strategy's entire point. Sharpe is
shown as `n/a` in contribution mode, because the equity curve jumps on each
deposit and those cash-flow jumps aren't investment returns — a Sharpe computed
from them would be meaningless.

All series in a sweep are aligned to a **common start date** — the warmup of
the *longest* MA tested — so every strategy and benchmark is measured over the
identical window and the numbers are directly comparable. (A single-MA run
aligns to that MA's own warmup.)

Example over 2011→2026 (calibrated spread), 100/150/161/200/250-day windows,
printed in the actual sorted order (best CAGR first):

| Rank | Series | Total return | CAGR | Max drawdown | Trades |
|------|--------|-------------:|-----:|-------------:|-------:|
| 1 | TQQQ Hold      |   +20,927%  | 43.8% | -81.7% |   1  |
| 2 | **161MA**      |   +16,523%  | **41.5%** | -55.0% |  85  |
| 3 | 200MA          |   +12,442%  | 38.8% | -55.0% |  69  |
| 4 | 150MA          |    +9,933%  | 36.7% | -59.6% | 103  |
| 5 | 250MA          |    +8,727%  | 35.5% | -61.1% |  79  |
| 6 | 100MA          |    +2,262%  | 23.9% | -58.1% | 157  |
| 7 | QQQ Hold       |    +1,245%  | 19.3% | -35.6% |   1  |

Among the strategies **161 wins** on return *and* ties for the lowest drawdown —
an empirical echo of the Reddit post's claim that 161 is a sweet spot. The
100-day is too twitchy (157 trades, worst return); the 250-day lags entries.

Options: `--ma` (one or more MA windows, default 161), `--threshold` (overheated %, default
0.05), `--no-overheating` (disable the +5% ballast rule entirely — deposits
above the MA always buy TQQQ), `--start` / `--end`, `--initial` (lump-sum mode), `--contribution`,
`--initial-lump-sum` (a day-one deposit in contributions mode, on top of the
recurring contributions), `--every` (trading days between deposits),
`--commission`, `--no-plot`, `--format` (chart format: `svg` default / `png` /
`pdf`), `--dpi` (raster resolution, only used for `--format png`).

Charts are written to `results/` as **SVG by default** — vector graphics that
stay razor-sharp at any zoom level (open in a browser or image viewer and zoom
freely). Use `--format png --dpi 300` if you specifically need a raster image.

The `--initial-lump-sum` deposit follows the same strategy rules as any
contribution, and the DCA benchmarks receive the same day-one lump sum so the
IRR comparison stays apples-to-apples.

## Sample results

Lump-sum, 2011-01 → 2026-09 (synthetic data):

Using the calibrated −0.40% spread:

| Strategy       | Total return | CAGR   | Max drawdown | Trades |
|----------------|-------------:|-------:|-------------:|-------:|
| 161MA Strategy |    +13,207%  | 38.3%  |    -55.0%    |   97   |
| TQQQ Hold      |    +26,414%  | 44.8%  |    -81.7%    |    1   |
| QQQ Hold       |     +1,391%  | 19.6%  |    -35.6%    |    1   |

The strategy's headline is **risk management**: it gives up some of TQQQ's raw
upside but roughly halves the worst drawdown (-55% vs -82%) by sitting in cash
during downtrends.

## Reading the annualized number: CAGR vs IRR

The two modes report annualized return differently, because a single day-one
investment and a stream of monthly deposits are not comparable:

- **Lump-sum mode** reports **CAGR** — the geometric growth of a single amount
  invested on day one.
- **Contribution mode** reports **IRR** (money-weighted / XIRR) — it accounts
  for *when* each deposit went in. A naive CAGR here is meaningless: dollars
  added last year haven't had time to compound, so dividing final by the first
  deposit wildly overstates the rate.

**The formulas**

```
CAGR = (final / initial) ^ (1 / years) - 1          # one flow, on day one

IRR solves for r in:                                # many flows, dated
    sum_i ( deposit_i / (1 + r) ^ years_i ) = final_value
```

CAGR looks only at the start value, end value, and elapsed time. IRR weights
each deposit by *how long it was actually invested*, via the `years_i` exponent.

**Worked example**

You invest **$1,000 on day one**, then add **$1,000 one year later**, and the
account is worth **$2,300** at the end of year 2.

- *Naive CAGR* (treating it as if $1,000 were the whole starting stake):
  `(2300 / 1000) ^ (1/2) - 1 = +51.7%/yr`. This is nonsense — it credits the
  second $1,000 with two years of growth it never had.
- *IRR* solves `1000 + 1000/(1+r) = 2300/(1+r)^2`, giving **≈ +9.7%/yr**
  (verified with this project's `xirr`). Each deposit is discounted from its own
  date: the first $1,000 is invested for 2 years, the second for only 1, and the
  ending value is discounted 2 years back to the first deposit.

Same account, same ending value — but IRR is the honest annual rate because it
respects when the money showed up. When there is only a *single* day-one
deposit, `years_i` is the same for the one flow and IRR collapses back to CAGR;
that is why lump-sum mode can safely report CAGR.

In contribution mode the benchmarks (`TQQQ DCA`, `QQQ DCA`) use the **same
deposit schedule** as the strategy, so all three IRRs are apples-to-apples.

Sanity check: buy-and-hold TQQQ should have a *higher* IRR than the strategy
(it's more aggressive) but a far *worse* max drawdown. If you ever see the
strategy with both a lower total return and a higher annualized return than
TQQQ, that's the old CAGR bug, not alpha.

## Notes & limitations

- Synthetic TQQQ omits bid/ask spread and intraday rebalancing slippage.
- Signals act on the **next** close after the cross to avoid look-ahead bias.
- IRR is solved by bisection (dependency-free); it returns `nan` for
  degenerate all-loss flow sets.
- Past backtested performance says nothing about the future. Leveraged ETFs can
  lose most of their value in a sharp downturn.
