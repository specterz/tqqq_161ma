# TQQQ_161ma — AGITQ 161-day MA Strategy

A rules-based TQQQ trend-following strategy and backtest, based on the r/TQQQ
post ["The ultimate TQQQ strategy"](https://www.reddit.com/r/TQQQ/comments/1q886nf/the_ultimate_tqqq_strategy/).
The approach is popular in Korean investing communities and is attributed to
"AGITQ".

## The rules

The signal is driven by **QQQ** (the Nasdaq-100), not by TQQQ itself:

1. **Buy TQQQ** when QQQ closes **above** its **161-day** simple moving average.
2. **Sell to treasury (SGOV / cash)** when QQQ closes **below** the 161-day MA.
3. **Overheated / ballast rule (optional, OFF by default):** enable it with
   `--overheating X`. Then new contributions buy TQQQ **only while** QQQ is above
   the MA **and** below MA+X%. Above MA+X% (overheated), the deposit is parked in
   a **VOO / S&P 500** sleeve instead. This ballast cushions drawdowns. Without
   the flag, any deposit above the MA buys TQQQ.
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
- **VOO / S&P 500 ballast sleeve** — the overheated rule parks deposits here.
  It grows at **real S&P 500 returns** from `data/SPX.csv` (Yahoo `^GSPC`,
  auto-updated like NDX), normalised to a $100 base. VOO tracks the S&P 500, so
  this is a faithful proxy for the ballast.

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
│   ├── NDX.csv               # Nasdaq-100 daily index (1985-2026)
│   ├── SPX.csv               # S&P 500 daily index (VOO ballast source)
│   ├── tbill_dgs3mo.csv      # 3-month T-bill rate (FRED DGS3MO)
│   └── TQQQ_real_yahoo.json  # real TQQQ closes (for calibration only)
├── src/
│   ├── data.py               # load data, build QQQ / synthetic TQQQ / rf / VOO
│   ├── strategy.py           # 161-day MA signal + overheated rule
│   ├── backtest.py           # lump-sum & contribution engines + metrics
│   ├── calibrate.py          # fit the financing spread to real TQQQ
│   ├── fetch_data.py         # download/refresh NDX + SPX from Yahoo
│   ├── web_api.py            # JSON bridge for the browser app (mirrors run.py)
│   ├── daily_signal.py       # daily action -> Discord/email alert
│   └── run.py                # CLI + chart output
├── docs/                     # Pyodide web app (GitHub Pages) + guides
│   ├── index.html, app.js, styles.css   # mobile-friendly browser backtester
│   ├── py/, data/            # engine + data synced here by build_web.py
│   ├── DAILY_SIGNAL.md        # daily-alert setup
│   └── CODE_WALKTHROUGH.md    # function-by-function tour
├── tradingview/              # Pine Script ports (lump-sum + contributions)
├── .github/workflows/        # daily-signal cron (fetch, alert, commit data)
├── build_web.py              # sync src/*.py + data into docs/ for the web app
└── results/                  # generated charts
```

## Usage

```bash
pip install -r requirements.txt

# Lump-sum backtest from 2011 (approx TQQQ era)
python src/run.py --mode lump_sum --start 2011-01-01

# Dollar-cost-averaging (overheating rule off by default)
python src/run.py --mode contributions --contribution 100 --every 21 --start 2020-01-01

# Same, but enable the +5% overheated rule (deposits above MA+5% go to VOO/S&P)
python src/run.py --mode contributions --contribution 100 --every 21 --overheating 5

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
+----------------+----------+------------+-----------+--------+--------+--------+---------+-------+----------+------+
| Strategy       | Invested |      Final |     Total |   CAGR | Sharpe |  MaxDD | Entries | Exits | Contribs | Comm |
+----------------+----------+------------+-----------+--------+--------+--------+---------+-------+----------+------+
| TQQQ Hold      |  $10,000 | $2,095,519 | +20855.2% | +43.7% |   0.87 | -81.7% |       1 |     0 |        0 |   $0 |
| 161MA Strategy |  $10,000 | $1,656,612 | +16466.1% | +41.4% |   0.94 | -55.0% |      43 |    42 |        0 |   $0 |
| 200MA Strategy |  $10,000 | $1,249,952 | +12399.5% | +38.7% |   0.90 | -55.0% |      35 |    34 |        0 |   $0 |
| 150MA Strategy |  $10,000 |   $999,838 |  +9898.4% | +36.7% |   0.87 | -59.6% |      52 |    51 |        0 |   $0 |
| 250MA Strategy |  $10,000 |   $879,651 |  +8696.5% | +35.5% |   0.84 | -61.1% |      40 |    39 |        0 |   $0 |
| 100MA Strategy |  $10,000 |   $235,418 |  +2254.2% | +23.9% |   0.67 | -58.1% |      79 |    78 |        0 |   $0 |
| QQQ Hold       |  $10,000 |   $134,386 |  +1243.9% | +19.3% |   0.87 | -35.6% |       1 |     0 |        0 |   $0 |
+----------------+----------+------------+-----------+--------+--------+--------+---------+-------+----------+------+
```

The header adapts to the metric (CAGR vs IRR) and the columns auto-size.

**How to read the trade columns.** Trade activity is broken out into three
separate counts rather than one lumped "trades" number, so you can see *what
kind* of activity drove the result:

- **Entries** — signal **buys**: QQQ crossed **above** its MA, so held capital
  (or parked cash) rotated **into** TQQQ. Buy-and-hold benchmarks show `1` (the
  single inception buy); DCA benchmarks show `0` (they never rotate on signal).
- **Exits** — signal **sells**: QQQ crossed **below** its MA, so TQQQ was
  rotated **out** to cash (SGOV). For the strategy, Entries and Exits track each
  other closely — each round-trip is one of each (the trailing count can differ
  by one when the series ends mid-position).
- **Contribs** — scheduled **deposits** (contributions mode only). These are
  cash inflows on the deposit schedule, **not** signal rotations, so they're
  counted separately and never inflate the entry/exit trade count. Lump-sum
  runs show `0` here.

The older single `Trades` column was `Entries + Exits` combined; splitting it
makes the round-trip count and the deposit cadence legible at a glance. The
`.trades` property on a result still returns the combined `entries + exits`
total if you need the old single number in code.

**Sharpe ratio** is the annualized risk-adjusted return: mean daily excess
return (over the T-bill) divided by daily volatility, times √252. Higher is
better per unit of risk. Note that here **161MA has the best Sharpe of the whole
table (0.94 in the sweep above) — beating buy-and-hold TQQQ (0.87)**: it
captures most of TQQQ's return with far less volatility, which is the strategy's
entire point. Sharpe is shown as `n/a` in contribution mode, because the equity
curve jumps on each deposit and those cash-flow jumps aren't investment returns
— a Sharpe computed from them would be meaningless. (Exact Sharpe values shift
slightly as the daily data updates; the *ranking* is the durable takeaway.)

All series in a sweep are aligned to a **common start date** — the warmup of
the *longest* MA tested — so every strategy and benchmark is measured over the
identical window and the numbers are directly comparable. (A single-MA run
aligns to that MA's own warmup.)

Example over 2011→2026 (calibrated spread), 100/150/161/200/250-day windows,
printed in the actual sorted order (best CAGR first):

| Rank | Series | Total return | CAGR | Max drawdown | Entries | Exits |
|------|--------|-------------:|-----:|-------------:|--------:|------:|
| 1 | TQQQ Hold      |   +20,855%  | 43.7% | -81.7% |   1  |  0  |
| 2 | **161MA**      |   +16,466%  | **41.4%** | -55.0% |  43  | 42  |
| 3 | 200MA          |   +12,400%  | 38.7% | -55.0% |  35  | 34  |
| 4 | 150MA          |    +9,898%  | 36.7% | -59.6% |  52  | 51  |
| 5 | 250MA          |    +8,697%  | 35.5% | -61.1% |  40  | 39  |
| 6 | 100MA          |    +2,254%  | 23.9% | -58.1% |  79  | 78  |
| 7 | QQQ Hold       |    +1,244%  | 19.3% | -35.6% |   1  |  0  |

Among the strategies **161 wins** on return *and* ties for the lowest drawdown —
an empirical echo of the Reddit post's claim that 161 is a sweet spot. The
100-day is too twitchy (79 entries / 78 exits — the most round-trips, worst
return); the 250-day lags entries.

### Complete parameter reference

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--mode` | `lump_sum` / `contributions` | `lump_sum` | Invest once (lump-sum) or dollar-cost-average periodically. |
| `--start` | `YYYY-MM-DD` | earliest data | Backtest start date. |
| `--end` | `YYYY-MM-DD` | latest data | Backtest end date. |
| `--initial` | float | `10000` | Starting capital (lump-sum mode). |
| `--contribution` | float | `100` | Deposit amount (contributions mode). |
| `--initial-lump-sum` | float | `0` | Day-one lump sum *plus* recurring deposits (contributions mode). |
| `--every` | int | `21` | Trading days between deposits (~21 = monthly, 5 = weekly, 1 = daily). |
| `--commission` | float | `0` | Per-rotation commission ($). Contributions mode defaults to 2 if 0. |
| `--ma` | int(s) | `161` | One or more MA windows. Pass several to compare: `--ma 100 161 200`. |
| `--overheating` | float | *off* | Enable the ballast rule with a +X% band. Above MA+X%, deposits go to VOO instead of TQQQ. Omit = rule off. |
| `--leverage` | float(s) | `3` | Leveraged-ETF factor: 3 = TQQQ, 2 = QLD, 1 = QQQ. Multiple: `--leverage 2 3`. |
| `--financing-spread` | float | `-0.004` | Financing spread over T-bill (calibrated for 3x). |
| `--expense-ratio` | float | `0.0095` | Annual fund expense ratio. |
| `--mom` | flag | off | Print a month-over-month return table (year×month grid + Avg footer). |
| `--no-plot` | flag | off | Skip chart generation. |
| `--format` | `svg`/`png`/`pdf` | `svg` | Chart file format. SVG = vector (infinite zoom). |
| `--dpi` | int | `200` | Raster DPI (only for `--format png`). |
| `--no-update` | flag | off | Offline mode: skip auto-download/refresh of NDX/SPX data. |
| `--max-age-days` | float | close-aware | Calendar-day staleness rule. `0` = always refetch. Default = refetch after today's close. |

`--ma` and `--leverage` combine as a full cross-product: `--ma 161 200 --leverage 2 3`
produces four strategy rows (QLD 161MA, QLD 200MA, TQQQ 161MA, TQQQ 200MA),
each leverage's Hold/DCA benchmark, and a shared QQQ benchmark. The browser app
runs one leverage at a time — use the CLI for multi-leverage sweeps.

### Examples — every parameter in action

```bash
# --- Mode -------------------------------------------------------------------
# Lump-sum: invest $10,000 once, rotate TQQQ <-> cash on the 161-MA signal.
python src/run.py --mode lump_sum --start 2011-01-01

# Contributions (DCA): deposit $100 every 21 trading days (~monthly).
python src/run.py --mode contributions --start 2015-01-01

# --- Start / end dates -------------------------------------------------------
# Backtest only 2020-2023 (the COVID crash + recovery).
python src/run.py --mode lump_sum --start 2020-01-01 --end 2023-12-31

# --- Initial capital (lump-sum) -----------------------------------------------
# Start with $50,000 instead of the default $10,000.
python src/run.py --mode lump_sum --initial 50000 --start 2011-01-01

# --- Contribution + every (DCA) -----------------------------------------------
# $200 every 5 trading days (~weekly).
python src/run.py --mode contributions --contribution 200 --every 5 --start 2015-01-01

# $50 every single trading day.
python src/run.py --mode contributions --contribution 50 --every 1 --start 2020-01-01

# --- Initial lump sum (DCA + day-one deposit) ---------------------------------
# Start with $10,000 on day one, PLUS $100/month ongoing.
python src/run.py --mode contributions --initial-lump-sum 10000 --contribution 100 --every 21

# --- Commission ---------------------------------------------------------------
# $5 per signal rotation (entry or exit).
python src/run.py --mode lump_sum --commission 5 --start 2011-01-01

# --- MA sweep -----------------------------------------------------------------
# Compare 5 MA windows side by side, sorted best-to-worst.
python src/run.py --mode lump_sum --ma 100 150 161 200 250 --start 2011-01-01

# Single alternative: the classic 200-day MA.
python src/run.py --mode lump_sum --ma 200 --start 2011-01-01

# --- Overheating (ballast rule, off by default) --------------------------------
# Enable with a +5% band: above MA+5%, deposits go to VOO instead of TQQQ.
python src/run.py --mode contributions --overheating 5 --start 2011-01-01

# Tighter +3% band: more deposits diverted to VOO, shallower drawdown.
python src/run.py --mode contributions --overheating 3 --start 2011-01-01

# --- Leverage ------------------------------------------------------------------
# Model QLD (2x) instead of TQQQ (3x).
python src/run.py --mode lump_sum --leverage 2 --start 2011-01-01

# Compare 2x and 3x side by side.
python src/run.py --mode lump_sum --leverage 2 3 --start 2011-01-01

# Full cross-product: 2 leverages × 2 MAs = 4 strategy rows.
python src/run.py --mode lump_sum --leverage 2 3 --ma 161 200 --start 2011-01-01

# --- Financing spread / expense ratio -----------------------------------------
# Conservative: +0.50% borrow assumption (original pre-calibration value).
python src/run.py --mode lump_sum --financing-spread 0.005 --start 2011-01-01

# What if the fund were cheaper (0.50% expense ratio)?
python src/run.py --mode lump_sum --expense-ratio 0.005 --start 2011-01-01

# --- Month-over-month table ---------------------------------------------------
# Print the monthly returns grid (year × month + Avg footer) for the best strategy.
python src/run.py --mode lump_sum --start 2020-01-01 --mom

# MoM works with contributions and MA sweeps too.
python src/run.py --mode contributions --start 2020-01-01 --ma 161 200 --mom

# --- Chart format + DPI -------------------------------------------------------
# Default is SVG (vector, infinite zoom); switch to PNG at 300 DPI.
python src/run.py --mode lump_sum --start 2011-01-01 --format png --dpi 300

# PDF output.
python src/run.py --mode lump_sum --start 2011-01-01 --format pdf

# Skip chart generation entirely.
python src/run.py --mode lump_sum --start 2011-01-01 --no-plot

# --- Data freshness -----------------------------------------------------------
# Offline: use cached data, never touch the network.
python src/run.py --mode lump_sum --start 2011-01-01 --no-update

# Force a refetch even if data is fresh (max-age-days 0 = always stale).
python src/run.py --mode lump_sum --start 2011-01-01 --max-age-days 0

# Consider data fresh for 7 days.
python src/run.py --mode lump_sum --start 2011-01-01 --max-age-days 7

# --- Kitchen sink: many flags combined ----------------------------------------
# DCA, $500/month + $20k lump sum, QLD + TQQQ, 161 + 200 MA, overheating 5%,
# $2 commission, MoM table, start 2015.
python src/run.py --mode contributions --contribution 500 --every 21 \
    --initial-lump-sum 20000 --leverage 2 3 --ma 161 200 --overheating 5 \
    --commission 2 --mom --start 2015-01-01
```

Charts are written to `results/` as **SVG by default** — vector graphics that
stay razor-sharp at any zoom level (open in a browser or image viewer and zoom
freely). Use `--format png --dpi 300` if you specifically need a raster image.

The `--initial-lump-sum` deposit follows the same strategy rules as any
contribution, and the DCA benchmarks receive the same day-one lump sum so the
IRR comparison stays apples-to-apples.

## Sample results

Lump-sum, 2011-01 → 2026-09 (synthetic data):

Using the calibrated −0.40% spread:

| Strategy       | Total return | CAGR   | Max drawdown | Entries | Exits |
|----------------|-------------:|-------:|-------------:|--------:|------:|
| 161MA Strategy |    +13,162%  | 38.2%  |    -55.0%    |   49    |  48   |
| TQQQ Hold      |    +26,323%  | 44.7%  |    -81.7%    |    1    |   0   |
| QQQ Hold       |     +1,390%  | 19.6%  |    -35.6%    |    1    |   0   |

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
