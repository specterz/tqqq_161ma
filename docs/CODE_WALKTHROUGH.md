# Code walkthrough

A guided, function-by-function tour of the `src/` modules, with line-level
detail on the parts that carry real reasoning (the leveraged-ETF model, the
look-ahead-safe signal, the XIRR solver, the three-sleeve DCA loop, and the
close-aware data refresh). Trivial plumbing is summarised, not narrated
line-by-line.

For the *what it does from the outside* view, see the top-level
[`README.md`](../README.md). This document is the *how and why on the inside*.

## Reading order

The modules depend on each other in this order — read top to bottom:

```
fetch_data.py   → refreshes data/NDX.csv from Yahoo (called by data.py)
data.py         → loads CSVs, builds QQQ / synthetic TQQQ / risk-free series
strategy.py     → turns the QQQ series into daily buy/sell/ballast signals
backtest.py     → runs the signals through equity engines + computes metrics
run.py          → CLI that wires all of the above together and plots results
```

A single `python src/run.py` call flows right-to-left through that list:
`run` asks `data` to `build_market_data` (which calls `fetch_data` to refresh),
hands the QQQ series to `strategy.compute_signals`, then feeds market data +
signals into a `backtest` engine and renders the result.

---

## `data.py` — data loading and synthetic ETF construction

This module answers one question: *given a long Nasdaq-100 index history, what
did QQQ, a 3x-daily TQQQ, and a cash/T-bill sleeve do each day?* The strategy's
signal only needs QQQ, but the backtest needs all three to simulate rotating
between them.

### Module constants

```python
TQQQ_LEVERAGE = 3.0          # TQQQ targets 3x the daily index return
TQQQ_EXPENSE_RATIO = 0.0095  # 0.95%/yr — ProShares TQQQ's real expense ratio
FINANCING_SPREAD = -0.0040   # -0.40%, calibrated against real TQQQ (calibrate.py)
TRADING_DAYS = 252           # trading days per year; annualisation denominator
```

`FINANCING_SPREAD` being *negative* is the surprising one: it means real TQQQ
tracked 3x the index slightly *better* than a naive "borrow at the T-bill rate"
model predicts, thanks to securities-lending income and favourable swap terms.
It isn't a guess — `calibrate.py` fits it to real TQQQ closes (see the README's
calibration section).

### `MarketData` (dataclass)

A thin wrapper around a single DataFrame indexed by date, with columns
`qqq`, `tqqq`, `rf`. Two conveniences:

- `index` — shortcut to `frame.index` (the `DatetimeIndex`).
- `slice(start, end)` — return a **new** `MarketData` restricted to a date
  range. It copies, so slicing never mutates the original frame. Both bounds
  are optional and inclusive.

### `_load_ndx(path)`

Loads the Nasdaq-100 close as an ascending float `Series`. The investing.com
export quotes the close under a `Price` column with thousands separators
(`"20,123.45"`), so the line

```python
price = raw["Price"].astype(str).str.replace(",", "", regex=False).astype(float)
```

forces text, strips commas, then parses to float — doing it in that order
avoids pandas guessing the dtype wrong on the comma-formatted strings. The
series is sorted ascending and de-duplicated (`keep="last"`) so a repeated date
resolves to its most recent value.

### `_load_riskfree(path)`

Loads FRED's DGS3MO (the 3-month T-bill rate, in percent) as a **daily decimal**
series. Key steps:

- `pd.to_numeric(..., errors="coerce") / 100.0` — convert percent to decimal;
  `coerce` turns FRED's blank holiday rows into `NaN` instead of crashing.
- `.ffill()` — carry the last known rate forward across those blanks, because a
  T-bill rate is "still in effect" on a day FRED didn't publish.

### `build_market_data(...)` — the core

This is where the three series are constructed and aligned. Walking the body:

```python
ensure_ndx_csv(ndx_path, max_age_days=max_age_days, auto_update=auto_update)
```
Refresh the index CSV first (downloads if missing/stale; no-op if fresh or if
`auto_update=False`). Imported lazily inside the function to avoid a circular
import (`fetch_data` doesn't import `data` at module load).

```python
rf = rf.reindex(ndx.index).ffill().bfill()
```
Put the risk-free rate onto the **index's** trading calendar. `reindex` aligns
to NDX dates; `ffill` covers gaps; `bfill` covers the rare case where the very
first NDX date precedes the first available rate.

```python
qqq = ndx / ndx.iloc[0] * 100.0
```
QQQ proxy: normalise the index so it starts at $100. This tracks the index 1:1
(QQQ *is* the Nasdaq-100), but rebasing to $100 makes it read like an ETF price.

```python
r_index = ndx.pct_change().fillna(0.0)
```
The index's daily simple return. Day one has no prior day, so `fillna(0.0)`
makes it flat rather than `NaN`.

Now the leveraged-ETF model — the heart of the module:

```python
daily_financing = (leverage - 1.0) * (rf + financing_spread) / TRADING_DAYS
daily_expense   = expense_ratio / TRADING_DAYS
r_tqqq = leverage * r_index - daily_financing - daily_expense
```
Read this as the standard daily-reset leveraged-ETF path model:

- `leverage * r_index` — 3x the index's daily move (the headline exposure).
- `daily_financing` — a 3x ETF holds 1x in cash and borrows the other **2x**
  (`leverage - 1`). It pays interest on that borrowed notional at the T-bill
  rate plus the financing spread, divided by 252 to get a daily drag.
- `daily_expense` — the 0.95% annual fee, also spread across 252 days.

This captures **volatility drag** automatically: because it compounds *daily*
returns, an up-then-down index leaves 3x TQQQ lower than 3x the index's net
move — exactly how real leveraged ETFs behave. What it intentionally omits is
bid/ask spread and intraday rebalancing slippage (second-order for a
daily-checked trend strategy).

```python
r_tqqq.iloc[0] = 0.0
tqqq = (1.0 + r_tqqq).cumprod() * 100.0
```
Force day one flat, then compound the daily returns into a price path starting
at $100 — same $100 base as QQQ so the two are visually comparable.

The function returns a `MarketData` with `qqq`, `tqqq`, and the annualised `rf`
(kept annualised; the backtest converts it to a daily growth factor where
needed). A final `dropna()` drops any row where alignment left a gap.

---

## `strategy.py` — the 161-day MA signal

The smallest module, and the one that encodes the actual trading rules. It turns
a QQQ price series into a per-day target allocation. It does **no** backtesting
itself — it only decides, for each day, what regime we're in.

### Module constants

```python
MA_WINDOW = 161            # the moving-average length, in trading days
OVERHEATED_THRESHOLD = 0.05  # +5% above the MA = "overheated" / ballast zone
```

### `Signals` (dataclass)

Wraps the output `DataFrame` (columns `qqq`, `ma`, `above`, `overheated`,
`target`) with an `index` shortcut. Keeping it in a typed container makes the
handoff to the backtest explicit — the engine knows exactly which columns exist.

### `compute_signals(qqq, ma_window=161, overheated_threshold=0.05)`

The whole rule set, four lines of logic:

```python
ma = qqq.rolling(window=ma_window, min_periods=ma_window).mean()
```
The trailing simple moving average. `min_periods=ma_window` is deliberate: the
MA is `NaN` until there are a full 161 observations, so we never trade on a
half-formed average during warmup.

```python
above = qqq > ma
overheated = qqq > ma * (1.0 + overheated_threshold)
```
The two regime flags. `above` drives the core hold/exit decision; `overheated`
(more than +5% above the MA) is the ballast flag that blocks *new* money in
contributions mode. Note `overheated` implies `above` — being 5% over the MA is
by definition above it.

```python
target = np.where(above, "TQQQ", "CASH")
```
The held-capital target for lump-sum mode: in TQQQ when above the MA, otherwise
in cash (SGOV). The overheated rule doesn't change the *held* target (an
existing position keeps riding); it only affects where new deposits go, which
the contributions engine handles using the `overheated` column directly.

```python
frame = frame.dropna(subset=["ma"])
```
Drop the warmup rows where the MA is undefined. After this, every row in the
returned frame is a genuine, fully-warmed signal.

**On look-ahead bias:** `compute_signals` computes everything on the close of
day *t*. It does **not** shift anything here. The shift lives in the backtest
engines (`.shift(1)`), so the signal from day *t* is only acted on at day
*t+1*'s close. Keeping the shift in the engine (not the signal) means the raw
`above`/`overheated` columns stay aligned to the day they were actually true,
which is what the plotting and monthly tables want.

---

## `fetch_data.py` — self-refreshing Nasdaq-100 data

Keeps `data/NDX.csv` current by downloading from Yahoo Finance's public chart
JSON API, with no third-party dependencies (stdlib `urllib` only). The design
goals: refresh only when needed, never break an offline run, and never
re-download pointlessly.

### Constants

```python
MARKET_CLOSE_HHMM = (16, 0)  # US regular close, 16:00 ET
PUBLISH_LAG_MIN = 30         # give the vendor 30 min after close to publish
YAHOO_SYMBOL = "%5ENDX"      # URL-encoded ^NDX (the Nasdaq-100 index)
```

`^NDX` is the index itself, not the QQQ ETF — this keeps the CSV a true index
series matching the bundled long history.

### `_fetch_chart_json(symbol, start_ts, end_ts)`

Builds the chart URL and does the HTTP GET with a browser-like `User-Agent`
(Yahoo rejects some default agents). 30-second timeout. Returns parsed JSON.

### `_chart_to_frame(payload)`

Digs the timestamp array and the close array out of Yahoo's nested JSON and
builds a two-column `Date`/`Price` frame:

```python
dates = pd.to_datetime(ts, unit="s", utc=True).tz_convert(None).normalize()
```
Yahoo returns Unix seconds. Parse as UTC, drop the timezone, and `normalize()`
to midnight so each row is a clean calendar date. Then drop NaN closes and
de-dup by date.

### `_write_ndx_csv` / `_read_existing`

Write in the exact investing.com-style shape `_load_ndx` expects (quoted,
comma-thousands, newest-first) so the downloaded file and the original bundled
file are interchangeable. `_read_existing` is the inverse and returns `None` on
any failure — callers treat `None` as "no usable cache".

### `latest_expected_session(now_et=None)` — the close-aware clock

This is the clever bit that makes staleness *smart* instead of a dumb
calendar-day count. It answers: "what's the most recent trading date whose bar
*should* already be published?"

- On a weekday after ~16:30 ET → **today** (today's bar is out).
- On a weekday before the close → **yesterday** (today's bar isn't out yet, so
  don't expect it).
- On a weekend → the preceding **Friday**.

```python
if not after_close:
    d = d - timedelta(days=1)
while d.weekday() >= 5:      # Sat=5, Sun=6
    d = d - timedelta(days=1)
```
Step back a day if today's close+lag hasn't passed, then roll back over any
weekend. It deliberately ignores exchange holidays — a holiday just yields "no
new rows", which is harmless. If `zoneinfo` is unavailable it approximates ET as
UTC-5.

### `_is_stale(path, max_age_days)`

Two modes:

- `max_age_days is None` (default): **close-aware**. Stale when the newest
  cached row is older than `latest_expected_session()`. This is what makes
  "delete today's bar after the close" correctly trigger a refetch while *not*
  fetching intraday or on weekends.
- a number: simple calendar rule (`0` = always refetch).

### `download_ndx(...)`

Fetches the full history and, when `merge_existing=True`, concatenates it with
the cache and drops duplicate dates **keeping the freshly fetched row**
(`keep="last"`). This preserves bundled history that predates Yahoo's `^NDX`
coverage while refreshing the recent tail. It returns
`(frame, new_row_count, latest_before, latest_after)` so callers can log exactly
what changed.

### `ensure_ndx_csv(...)` — the public entry point

The decision tree `build_market_data` relies on:

- `auto_update=False` → never touch the network; error only if the file is
  missing.
- missing **or** stale → try to download.
- fresh → no network call.
- download fails **but** a cache exists → fall back to the cache (log it).
- download fails **and** no cache → fatal `RuntimeError` (nothing to run on).

Every branch logs a one-line status, so a run always tells you whether data
changed. This is the "offline-safe" guarantee: the only unrecoverable case is a
first run with no data and no network.

---

## `backtest.py` — engines and metrics

The engine room. It takes `MarketData` + `Signals` and produces equity curves
and performance numbers. Two strategy engines (lump-sum and contributions) plus
matching buy-and-hold benchmarks, all returning the same `BacktestResult` shape
so the table formatter can treat them uniformly.

### `BacktestResult` (dataclass)

The common result container. Notable fields:

- `annualized` + `annualized_kind` — the annualised return **and** a label
  (`"CAGR"` or `"IRR"`) saying how it was computed. Carrying the kind alongside
  the number is what lets the table header adapt and prevents the classic bug of
  comparing a CAGR to an IRR as if they were the same thing.
- `entry_trades` / `exit_trades` / `contrib_trades` — the three trade counts the
  README documents. Kept separate so signal rotations never get conflated with
  scheduled deposits.
- `sharpe` and `daily_returns` — `daily_returns` is the *pure investment* return
  series (deposit inflows removed), stored so DCA mode can compute a meaningful
  Sharpe and monthly table despite the equity curve jumping on each deposit.

Two methods:

- `trades` property → `entry_trades + exit_trades` (the old combined count, for
  code that still wants one number).
- `monthly_returns()` → compounds daily returns within each calendar month.
  Prefers `daily_returns` when present (DCA), else derives from the equity curve
  (lump-sum) — either way it's the month's *investment* performance, not
  contaminated by deposits.
- `summary()` → a one-line text digest for quick logging.

### `format_results_table` / `format_monthly_table`

Pure text-formatting. They compute each column's width from the widest cell
(header, body, or footer), left-align the label column, right-align the numbers,
and draw the `+---+` box. `format_results_table` picks the annualized header
from the set of result kinds — one kind → `"CAGR"`/`"IRR"`, mixed → `"Ann."`.
`format_monthly_table` adds a YTD column (compounded, not summed) and an Avg
footer row (the mean return per calendar month).

### `_cagr(equity)`

Geometric compound annual growth rate:

```python
years = (equity.index[-1] - equity.index[0]).days / 365.25
return (end / start) ** (1.0 / years) - 1.0
```
Valid **only** for a single day-one investment. The docstring says so loudly,
because applying CAGR to a contribution stream is the exact bug `xirr` exists to
prevent.

### `xirr(dates, amounts)` — money-weighted return

The honest annualised return for dated cash flows. Sign convention: deposits are
**negative** (cash leaving your pocket), the final value is one **positive**
flow. It solves for the rate `r` that makes the net present value zero:

```
sum_i ( amount_i / (1 + r) ** years_i ) == 0
```

Why bisection instead of scipy: it's dependency-free and robust. The method:

```python
if all(a <= 0) or all(a >= 0):   # never crosses zero → no solvable rate
    return nan
```
Guard against degenerate flows (all-in or all-out).

```python
lo, hi = -0.9999, 10.0
f_lo, f_hi = npv(lo), npv(hi)
if f_lo * f_hi > 0:
    return nan
```
NPV is monotonically decreasing in `r`, so if the function has the **same sign**
at both ends it never crosses zero in the bracket — unsolvable, return `NaN`
(this catches all-loss series). Otherwise the root is bracketed and 200
bisection halvings converge to it. `npv` guards `1 + rate <= 0` by returning
`inf` so a rate below -100% can't blow up the math.

### `_max_drawdown(equity)`

```python
drawdown = equity / equity.cummax() - 1.0
return float(drawdown.min())
```
The worst peak-to-trough decline: divide each point by the running maximum so
far, take the most negative value. `cummax` is the running peak.

### `_sharpe` / `_sharpe_from_returns`

Annualised Sharpe = `mean(excess daily return) / std(daily return) * sqrt(252)`,
where excess subtracts the daily T-bill rate. Two variants:

- `_sharpe(equity, rf)` derives daily returns from an equity curve — correct for
  lump-sum and buy-and-hold, where the curve moves only from market returns.
- `_sharpe_from_returns(rets, rf)` takes a precomputed return series — used by
  DCA mode, whose equity curve is contaminated by deposit jumps, so the engine
  tracks a clean investment-return series separately and passes it here.

Both return `NaN` on `<2` points or zero volatility. This is why the README
shows Sharpe as `n/a` for contribution benchmarks computed from raw curves.

### `run_lump_sum(...)`

Invest once on day one and rotate held capital between TQQQ and cash.

```python
holding = frame["target"].shift(1).fillna("CASH")
```
**The look-ahead guard.** Today's *position* is yesterday's *signal*. Shifting
by one day means a signal computed on day *t*'s close is only acted on at day
*t+1*, so we never trade on information we couldn't have had. Day one defaults
to cash.

The loop walks day by day: if today's holding is TQQQ, grow equity by TQQQ's
daily return; else grow it by the cash (T-bill) factor. When the holding differs
from the previous day, it's a rotation — count it as an **entry** if moving into
TQQQ, an **exit** if moving to cash, and subtract commission. It then packages
the equity series into a `BacktestResult` with `annualized_kind="CAGR"`.

### `run_hold(...)`

The simplest benchmark: buy one asset on day one and hold. Equity is just the
price series rescaled to the initial investment. Recorded as `1` entry / `0`
exits — buy-and-hold buys once and never sells.

### `run_hold_dca(...)`

Dollar-cost-averaging into a single asset, used as the DCA benchmark. It buys a
fixed dollar amount every `every_n_days` (and an optional day-one lump sum),
accumulating shares. Crucially it uses the **same deposit schedule** as the
strategy so the IRR comparison is apples-to-apples. It records each deposit as a
negative cash flow and the final value as the positive flow, then calls `xirr`.
For a single held asset the daily investment return is just the asset's own
price return, so Sharpe uses `_sharpe_from_returns(asset_ret, rf)`.

### `run_contributions(...)` — the three-sleeve DCA engine

The most involved function: DCA *with* the overheated ballast rule. It tracks
three dollar balances — `tqqq_bal`, `cash_bal`, `spy_bal` (the S&P ballast
sleeve, approximated by the risk-free series when no SPY data is supplied).

Signals are shifted by one day (`above`, `overheated`) for the same
look-ahead-safe reason as lump-sum. The daily loop does four things **in order**:

1. **Grow** each sleeve by its daily return.
2. **Sell rule (exit):** if QQQ is below the MA and we hold TQQQ, move it all to
   cash, count an exit, pay commission.
3. **Re-entry (entry):** if QQQ is above the MA and not overheated and we have
   parked cash, move it into TQQQ, count an entry, pay commission. This is how a
   deposit parked during a downtrend gets deployed once the trend recovers.
4. **Deposit on schedule:** every `every_n_days`, add a contribution and route
   it by regime — TQQQ if above and not overheated, **ballast (SPY)** if above
   but overheated, cash if below the MA.

The subtle line is the daily-return bookkeeping:

```python
pre_deposit = tqqq_bal + cash_bal + spy_bal
if i > 0 and equity[i - 1] > 0.0:
    daily_ret[i] = pre_deposit / equity[i - 1] - 1.0
```
Portfolio value is measured **before** today's deposit is added, then compared
to yesterday's close. That isolates the *investment* return from the deposit
inflow — without this, every deposit day would look like a huge "return" and
corrupt the Sharpe. The clean `daily_ret` series is what gets passed to
`_sharpe_from_returns` and stored as `daily_returns`.

Like `run_hold_dca`, it logs dated flows and reports `annualized_kind="IRR"`.

---

## `run.py` — the CLI

The conductor. It parses arguments, builds the data, runs one strategy per
requested MA window plus the benchmarks, prints the sorted table, and draws the
chart.

### `parse_args()`

Standard `argparse`. The flags map directly to the README's Usage section:
`--mode`, `--start`/`--end`, `--initial`, `--contribution`/`--every`/
`--initial-lump-sum` (DCA), `--ma` (one or more windows), `--threshold` and
`--no-overheating` (the ballast rule), `--financing-spread`/`--expense-ratio`
(stress-test the TQQQ model), `--no-update`/`--max-age-days` (data refresh
control), and the chart flags `--no-plot`/`--format`/`--dpi`.

### `main()` — the flow

```python
md_full = build_market_data(**md_kwargs).slice(args.start, args.end)
```
Build all three price series (refreshing data as needed), then slice to the
requested window. Optional `financing_spread`/`expense_ratio` overrides are
only inserted into the kwargs when provided, so the calibrated defaults stand
otherwise.

**Common-start alignment** — the part that keeps a multi-MA sweep honest:

```python
warmup = max(ma_windows)
common_start = md_full.index[warmup - 1]
md = md_full.slice(common_start.strftime("%Y-%m-%d"))
```
A 250-day MA needs more warmup than a 100-day one, so if each strategy started
on its own first-valid day they'd be measured over **different** windows and the
numbers wouldn't be comparable. Instead everything — every strategy *and* every
benchmark — is aligned to the warmup date of the **longest** MA tested. Now all
series cover the identical window.

**Per-MA loop:** for each window it computes signals on the *full* series (so
the MA is fully warmed) then slices the signal frame to `common_start`, and runs
the chosen engine (`run_lump_sum` or `run_contributions`). Computing on the full
series then slicing — rather than computing on the already-sliced series — is
what guarantees each MA is genuinely warmed up rather than starting cold at
`common_start`.

**Benchmarks** are MA-independent, so they're built once over the common window:
`run_hold` for TQQQ and QQQ in lump-sum mode, `run_hold_dca` for both in
contributions mode (with the same deposit schedule).

**Sorting and reporting:**

```python
results = sorted(results, key=lambda r: r.annualized, reverse=True)
```
Strategies and benchmarks are interleaved and ranked best-to-worst by annualised
return (NaN sorts last). `format_results_table` prints the boxed table; the best
strategy is flagged; `--mom` optionally prints the monthly-returns grid.

**Plotting:** a two-panel figure — equity curves (log scale, strategies solid,
benchmarks dashed) on top, and a signal panel (QQQ vs each MA line) below.
`matplotlib.use("Agg")` at import time makes it headless (write a file, never
open a window), which is what lets it run on a server or in CI. SVG is the
default output (vector, infinite zoom); PNG honours `--dpi`.

---

## How a single run flows through the modules

Putting it all together, `python src/run.py --mode lump_sum --start 2011-01-01`:

1. `run.main()` calls `build_market_data()`.
2. `data.build_market_data()` calls `fetch_data.ensure_ndx_csv()` → refreshes
   `NDX.csv` if stale, then builds QQQ / synthetic TQQQ / rf.
3. `run` slices to the window, computes `common_start`, and for the 161 MA calls
   `strategy.compute_signals()` → the daily `above`/`overheated`/`target` frame.
4. `run` feeds market data + signals into `backtest.run_lump_sum()` → an equity
   curve with CAGR, Sharpe, drawdown, and entry/exit counts.
5. `run` builds the TQQQ/QQQ hold benchmarks, sorts everything, prints the
   table, and writes `results/backtest_lump_sum.svg`.

That's the entire pipeline: **fetch → build → signal → backtest → report.**
