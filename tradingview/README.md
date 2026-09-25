# Backtesting the 161MA strategy on TradingView

Two Pine Script v6 ports of the strategy, both computing the signal on **QQQ**
and trading **TQQQ** via TradingView's Strategy Tester:

| File | Mode | Mirrors Python |
|------|------|----------------|
| `AGITQ_161MA_LUMP_SUM.pine` | Invest once, rotate 100% TQQQ ↔ cash | `--mode lump_sum` |
| `AGITQ_161MA_CONTRIBUTIONS.pine` | Dollar-cost-average a fixed $ every N days | `--mode contributions` |

Start with the **lump-sum** script — it maps cleanly to TradingView's tester.
The **contributions** script emulates recurring deposits (see its caveats
below), since Pine has no native "deposit every month" concept.

## Steps (either script)

1. In TradingView, open a **TQQQ** chart and switch to the **Daily (1D)**
   timeframe. (The 161-day MA only makes sense on daily bars.)
2. Open the **Pine Editor** (bottom panel), paste the contents of the `.pine`
   file, and click **Add to chart**.
3. For **lump-sum**: read the **Strategy Tester** tab (net profit, max drawdown,
   trades, equity curve). For **contributions**: read the **on-chart table**
   (top-right) — invested vs value vs return — because the tester's % column is
   measured against `initial_capital`, not total contributions.
4. Tune via the gear icon: MA length (161), overheated band (5%), signal symbol,
   and — for contributions — the deposit size and cadence.

## Seeing QQQ price + its 161-MA

The signal is QQQ (~$400-600) but you trade TQQQ (~$50-140). On a TQQQ chart the
*real* QQQ price/MA plot off-screen, and one Pine script can't open a second
pane. Two ways to view them:

- **Best — chart QQQ directly:** change the chart symbol to `NASDAQ:QQQ`, then in
  the lump-sum strategy's gear icon tick **"I'm charting QQQ (plot REAL QQQ price
  + MA)"**. Now the candles are QQQ and the true 161-MA + overheated band plot
  exactly on price. The trade signal is unchanged.
- **On a TQQQ chart (default):** leave that box off. The QQQ MA is *rescaled* by
  the live TQQQ/QQQ ratio so it tracks near the TQQQ candles — a visual aid; the
  actual signal still uses the true MA. The red/orange background always marks
  the regime regardless of chart symbol.
- **No-code alternative:** click the **+ (Compare/Add symbol)** on any chart, add
  `NASDAQ:QQQ`, and drop a 161-period SMA on it. Native TradingView, no script.

## Contributions mode: important caveats

Pine's Strategy Tester models a single capital pool with no built-in recurring
deposit, so `AGITQ_161MA_CONTRIBUTIONS.pine` emulates DCA:

- It tracks contributed cash in a variable and buys TQQQ in **dollar amounts**
  (`qty_type = strategy.cash`) on the deposit schedule, gated by the 161-MA rule.
- The **on-chart table** is the honest performance read (invested / value /
  total return / rough annualized). The tester's built-in "Net Profit %" is
  relative to `initial_capital` and will look off — ignore it here.
- There is **no true IRR** in Pine; the table's "Approx ann." is a simple
  geometric figure for orientation only. For the exact money-weighted IRR, use
  the Python backtest: `python src/run.py --mode contributions --ma 161`.
- The overheated "ballast" is modelled as *hold cash* (don't buy TQQQ), not as
  buying an S&P sleeve — same simplification as the lump-sum script.

## How it maps to the rules

| Rule | Pine implementation |
|------|---------------------|
| Signal from QQQ, not TQQQ | `request.security("NASDAQ:QQQ", "D", close)` |
| Buy TQQQ when QQQ > 161 SMA | `strategy.entry` when `aboveMa` |
| Sell to cash when QQQ < 161 SMA | `strategy.close` when `not aboveMa` |
| Overheated (> MA + 5%) blocks new entry | `blockOnHot and overheated` |
| No look-ahead | `barmerge.lookahead_off` + `process_orders_on_close` |

When flat, the strategy is in cash — TradingView doesn't earn an explicit SGOV
yield on idle capital, so cash simply sits (see caveats).

## Important caveats (why numbers differ from the Python backtest)

1. **Cash earns nothing here.** Our Python model grows idle cash at the 3-month
   T-bill rate (the SGOV sleeve). TradingView's tester leaves flat capital at
   0%. During long downtrends this makes the TradingView result slightly lower.

2. **No S&P 500 "ballast" sleeve.** The overheated rule in Pine simply *blocks
   new TQQQ entries* while hot; it does not divert money into an S&P position
   (the tester trades one symbol). The Python `contributions` mode routes
   overheated deposits into a ballast sleeve. So the overheated rule is
   modelled as "stay flat" rather than "buy SPY".

3. **Real vs synthetic TQQQ.** TradingView uses *real* TQQQ prices (from 2010
   inception). Our Python backtest uses synthetic TQQQ derived from the
   Nasdaq-100 index, which lets it run pre-2010 but only approximates the real
   fund. Expect the calibrated Python numbers to be close but not identical over
   2010+.

4. **Lump-sum vs DCA.** `AGITQ_161MA_LUMP_SUM.pine` is a full-rotation model
   (`percent_of_equity = 100`): 100% in TQQQ or 100% in cash. For recurring
   deposits use `AGITQ_161MA_CONTRIBUTIONS.pine`. Compare each against the
   matching Python mode (`--mode lump_sum` / `--mode contributions`).

5. **Commissions/slippage.** Set these in the `strategy(...)` header or the
   tester's Properties tab to match your broker; the script defaults to 0.

## Sanity check

Run the Python lump-sum backtest over the real-TQQQ window and compare the
shape (not the exact figure) to TradingView:

```bash
python src/run.py --mode lump_sum --start 2010-02-11 --ma 161
```

Both should show the strategy tracking well below buy-and-hold TQQQ in raw
return but with a much shallower max drawdown — the strategy's whole point.

---

# Paper trading (forward validation)

**Backtesting ≠ paper trading.** Backtesting replays *history* instantly. Paper
trading runs the strategy *forward* on live prices with fake money — it can't
tell you about the past, only whether your signals and execution hold up in real
time. Use it as the step *after* a backtest looks good and *before* real money.

Because the AGITQ rule is checked once per day, paper trading it is low effort:
you (or an alert) only act on the daily close.

## Option A — TradingView Paper Trading (pairs with this script)

1. Add `AGITQ_161MA_LUMP_SUM.pine` to a **TQQQ daily** chart.
2. In the bottom **Trading Panel**, choose **Paper Trading** and connect. You
   start with a simulated cash balance.
3. Drive the orders one of two ways:
   - **Manual:** each day, if the strategy prints a Buy/Sell marker, place the
     matching paper order yourself. Fine for a once-a-day system.
   - **Alert-driven:** the script now fires `alert()` on entry/exit. Click the
     "..." on the strategy → **Add alert** → condition **"AGITQ 161MA"** →
     **"Any alert() function call"**. Alerts can post to the paper broker or
     just notify you (email/app/webhook).
4. Let it run for weeks/months and compare the paper equity curve against what
   the backtest expected over the same live window.

> Note: alerts on a `strategy()` fire on the *broker/emulator* fills. If you
> want pure notification alerts independent of the tester, you can also copy the
> logic into an `indicator()` version — ask if you want that variant.

## Option B — Broker paper accounts (closer to real fills)

If you plan to trade this at a real broker, paper-trade it there so fills,
fractional shares, and settlement behave realistically:

- **Alpaca** — free paper API. Good if you want to *automate*: a small script
  checks QQQ's 161-day SMA after the close and submits a paper TQQQ order.
- **Interactive Brokers** — has a paper account mirroring the live platform.
- **Thinkorswim (Schwab) paperMoney**, **Webull paper**, **Fidelity** — manual,
  click-to-trade simulators; you place the daily order by hand.

### Minimal automation idea (Alpaca-style)

A once-a-day job that reproduces the strategy live:

1. After the US market close, pull QQQ daily closes and compute the 161-day SMA.
2. If QQQ > SMA and you're flat → submit a paper **buy** TQQQ (target 100% of
   paper equity). If QQQ > SMA + 5% (overheated) and you're flat → skip / hold.
3. If QQQ < SMA and you hold TQQQ → submit a paper **sell** to cash.
4. Log the action and the resulting position.

This is the same decision tree as `src/backtest.py::run_lump_sum`, just executed
one bar at a time going forward. If you'd like, I can write this as a small
Python script against the Alpaca paper API (keys required, orders are simulated).

## What to watch during paper trading

- **Whipsaws near the line:** QQQ hovering around the 161MA causes rapid
  buy/sell flip-flops. The backtest counts these as trades; live they cost
  spread + commission and test your discipline.
- **Execution timing:** the backtest assumes you act on the *close*. Live, you
  either trade at/near the close or wait to the next open — note which and stay
  consistent.
- **Idle cash yield:** if your paper broker doesn't pay interest on cash, park
  it in a money-market/SGOV-like instrument to mirror the SGOV sleeve.
