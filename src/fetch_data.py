"""Download / refresh the Nasdaq-100 index data used by the backtest.

The strategy signal is computed on QQQ (the Nasdaq-100 proxy), so we fetch a
long daily history of the index from Yahoo Finance's public ``chart`` JSON API
and write it in the same CSV shape ``data.py`` already understands
(``"Date","Price"`` columns, so ``_load_ndx`` keeps working unchanged).

Design goals
------------
* **Self-refreshing:** ``ensure_ndx_csv`` downloads when the file is missing or
  stale (older than ``max_age_days``), and is a no-op otherwise.
* **Offline-safe:** any network/parse failure falls back to the existing cached
  CSV if one is present; only a *missing* file with no network is fatal.
* **No new dependencies:** uses ``urllib`` from the stdlib.

Symbol note
-----------
Yahoo exposes the Nasdaq-100 index as ``%5ENDX`` (``^NDX``). We request that so
the CSV stays a true index series (matching the bundled ``NDX.csv``). QQQ or any
other symbol can be passed explicitly.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo/tzdata missing
    _ET = None

# US market regular close is 16:00 ET. We treat a session's bar as "available"
# a little after the close to allow the data vendor time to publish.
MARKET_CLOSE_HHMM = (16, 0)
PUBLISH_LAG_MIN = 30

# Nasdaq-100 index on Yahoo (URL-encoded ^NDX). QQQ inception is 1999; the index
# itself goes back to 1985 but Yahoo's ^NDX history is shorter, so we keep the
# bundled NDX.csv as the long history and only *extend/refresh* the recent tail.
YAHOO_SYMBOL = "%5ENDX"
# S&P 500 index (URL-encoded ^GSPC) — the underlying VOO tracks. Used for the
# overheated "ballast" sleeve so it reflects real S&P 500 returns.
SPX_SYMBOL = "%5EGSPC"
YAHOO_CHART = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={p1}&period2={p2}&interval=1d"
)
_USER_AGENT = "Mozilla/5.0 (compatible; tqqq-backtest/1.0)"


def _fetch_chart_json(symbol: str, start_ts: int, end_ts: int) -> dict:
    url = YAHOO_CHART.format(symbol=symbol, p1=start_ts, p2=end_ts)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _chart_to_frame(payload: dict) -> pd.DataFrame:
    """Yahoo chart JSON -> DataFrame with a 'Date' and 'Price' (close) column."""
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    dates = pd.to_datetime(ts, unit="s", utc=True).tz_convert(None).normalize()
    df = pd.DataFrame({"Date": dates, "Price": closes}).dropna()
    df = df.drop_duplicates(subset="Date", keep="last").sort_values("Date")
    return df


def _write_ndx_csv(df: pd.DataFrame, path: Path) -> None:
    """Write in the investing.com-style shape _load_ndx expects.

    ``_load_ndx`` only reads the ``Date`` and ``Price`` columns and strips commas,
    so a plain two-column CSV (newest-first, to match the original) is fine.
    """
    out = df.sort_values("Date", ascending=False).copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    out["Price"] = out["Price"].map(lambda x: f"{x:,.2f}")
    out.to_csv(path, index=False, columns=["Date", "Price"], quoting=1)  # quote all


def _read_existing(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        raw = pd.read_csv(path)
        price = raw["Price"].astype(str).str.replace(",", "", regex=False).astype(float)
        dates = pd.to_datetime(raw["Date"])
        return (
            pd.DataFrame({"Date": dates, "Price": price})
            .dropna()
            .drop_duplicates(subset="Date", keep="last")
            .sort_values("Date")
        )
    except Exception:
        return None


def latest_expected_session(now_et: datetime | None = None) -> pd.Timestamp:
    """Most recent US trading-session date whose close+lag has passed.

    This is close-aware, not just a calendar-day count:

    * On a weekday *after* ~16:30 ET -> today (today's bar should be published).
    * On a weekday *before* the close -> the previous weekday (today's bar isn't
      out yet, so we shouldn't expect it).
    * On weekends -> the preceding Friday.

    It intentionally ignores exchange holidays (no static calendar here); a
    holiday just means the fetch returns "no new rows", which is harmless.
    """
    if _ET is not None:
        now_et = now_et or datetime.now(_ET)
    else:  # fall back to UTC-5 approximation if tzdata is unavailable
        now_et = now_et or (datetime.now(timezone.utc) - timedelta(hours=5))

    # True once we're past 16:00 ET + the publish lag, i.e. today's bar should
    # now exist at the vendor. Built by adding the lag to today's 16:00 and
    # comparing wall-clock times.
    close_t = time(MARKET_CLOSE_HHMM[0], MARKET_CLOSE_HHMM[1])
    after_close = now_et.time() >= (
        datetime.combine(now_et.date(), close_t) + timedelta(minutes=PUBLISH_LAG_MIN)
    ).time()

    d = now_et.date()
    # If today's close+lag hasn't passed yet, step back a day to start.
    if not after_close:
        d = d - timedelta(days=1)
    # Roll back over weekends (Sat=5, Sun=6) to the most recent weekday.
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return pd.Timestamp(d)


def _is_stale(path: Path, max_age_days: float | None) -> bool:
    """True if the cached data should be refreshed.

    Two modes:

    * ``max_age_days is None`` (default): **close-aware**. Stale when the newest
      cached row is older than the most recent *expected* trading session
      (:func:`latest_expected_session`). This is what makes "removed today's bar
      after the close" correctly trigger a refetch, while not fetching pointlessly
      intraday or on weekends.
    * ``max_age_days`` is a number: simple calendar-day rule. ``0`` = always
      refetch; otherwise stale when the newest row is older than that many days.
    """
    existing = _read_existing(path)
    if existing is None or existing.empty:
        return True

    last = existing["Date"].max().normalize()

    if max_age_days is None:
        return last < latest_expected_session()

    if max_age_days <= 0:
        return True
    age = (pd.Timestamp.now().normalize() - last).days
    return age > max_age_days


def download_ndx(
    path: Path | str,
    symbol: str = YAHOO_SYMBOL,
    start: str = "1985-01-01",
    merge_existing: bool = True,
) -> pd.DataFrame:
    """Download the index history and (optionally) merge with the cached CSV.

    Merging preserves the long bundled history (which may predate Yahoo's ^NDX
    coverage) while refreshing/extending the recent tail. Returns
    ``(written_frame, new_row_count, latest_before, latest_after)`` where
    ``new_row_count`` is how many *new dates* the fetch added versus the cache.
    """
    path = Path(path)
    start_ts = int(pd.Timestamp(start).timestamp())
    end_ts = int(datetime.now(timezone.utc).timestamp())

    fetched = _chart_to_frame(_fetch_chart_json(symbol, start_ts, end_ts))

    existing = _read_existing(path) if merge_existing else None
    prev_dates = set() if existing is None else set(existing["Date"])
    latest_before = None if existing is None or existing.empty else existing["Date"].max()

    if existing is not None and not existing.empty:
        # Concatenate cache + fetch, then drop duplicate dates keeping the LAST
        # occurrence. Because `fetched` is concatenated after `existing`, "last"
        # is the freshly fetched row -> overlapping dates get refreshed while any
        # bundled history that predates Yahoo's ^NDX coverage is preserved.
        combined = pd.concat([existing, fetched], ignore_index=True)
        combined = combined.drop_duplicates(subset="Date", keep="last")
        fetched = combined.sort_values("Date")

    # Count only dates that weren't in the cache before, so the log reports the
    # true number of *new* bars (not overlapping refreshes).
    new_rows = int(sum(1 for d in fetched["Date"] if d not in prev_dates))
    latest_after = fetched["Date"].max()

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_ndx_csv(fetched, path)
    return fetched, new_rows, latest_before, latest_after


def ensure_ndx_csv(
    path: Path | str,
    max_age_days: float | None = None,
    symbol: str = YAHOO_SYMBOL,
    auto_update: bool = True,
    verbose: bool = True,
    label: str = "NDX",
) -> Path:
    """Ensure ``path`` exists and is fresh, downloading only if needed.

    Generic over the index (``label`` is only used in log messages) so the same
    logic serves both the Nasdaq-100 (``^NDX``) and the S&P 500 (``^GSPC``).

    * Missing file -> download (fatal if the download also fails).
    * Stale file -> try to refresh; fall back to the cached copy if the network
      fails. Staleness is **close-aware** by default (``max_age_days=None``): a
      refetch triggers once the most recent trading session's close has passed
      and that bar is missing. Pass a number for a simple calendar-day rule.
    * Fresh file, or ``auto_update=False`` -> no network call.
    """
    path = Path(path)

    def log(msg: str) -> None:
        if verbose:
            print(f"[fetch_data] {msg}")

    if not auto_update:
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing and auto_update is disabled."
            )
        existing = _read_existing(path)
        latest = existing["Date"].max().date() if existing is not None and not existing.empty else "?"
        log(f"{label}: auto-update off; using cached data (latest {latest}).")
        return path

    missing = not path.exists()
    stale = _is_stale(path, max_age_days)

    if not missing and not stale:
        existing = _read_existing(path)
        latest = existing["Date"].max().date() if existing is not None and not existing.empty else "?"
        log(f"{label}: data already up to date (latest {latest}); no fetch needed.")
        return path

    reason = "missing" if missing else "stale"
    log(f"{label} data {reason}; fetching {symbol}...")
    try:
        _df, new_rows, latest_before, latest_after = download_ndx(path, symbol=symbol)
        before = latest_before.date() if latest_before is not None else "none"
        after = latest_after.date()
        if new_rows > 0:
            log(f"{label}: updated +{new_rows} new row(s), latest {before} -> {after}.")
        else:
            log(f"{label}: fetch succeeded but no new rows (latest still {after}).")
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        if missing:
            raise RuntimeError(
                f"Could not download {label} data and no cached file exists: {exc}"
            ) from exc
        existing = _read_existing(path)
        latest = existing["Date"].max().date() if existing is not None and not existing.empty else "?"
        log(f"{label}: update failed ({exc}); using cached data (latest {latest}).")
    return path


def ensure_spx_csv(
    path: Path | str,
    max_age_days: float | None = None,
    auto_update: bool = True,
    verbose: bool = True,
) -> Path:
    """Ensure the S&P 500 (``^GSPC``) CSV exists and is fresh. Thin wrapper over
    :func:`ensure_ndx_csv` with the S&P symbol and an "SPX" log label.
    """
    return ensure_ndx_csv(
        path, max_age_days=max_age_days, symbol=SPX_SYMBOL,
        auto_update=auto_update, verbose=verbose, label="SPX",
    )


if __name__ == "__main__":
    import argparse

    from data import DATA_DIR

    p = argparse.ArgumentParser(description="Download/refresh NDX index data")
    p.add_argument("--path", default=str(DATA_DIR / "NDX.csv"))
    p.add_argument("--symbol", default=YAHOO_SYMBOL)
    p.add_argument("--max-age-days", type=float, default=None)
    p.add_argument("--force", action="store_true", help="download regardless of age")
    args = p.parse_args()

    if args.force:
        _df, new_rows, latest_before, latest_after = download_ndx(
            args.path, symbol=args.symbol
        )
        before = latest_before.date() if latest_before is not None else "none"
        print(
            f"[fetch_data] forced download: +{new_rows} new row(s), "
            f"latest {before} -> {latest_after.date()}."
        )
    else:
        ensure_ndx_csv(args.path, max_age_days=args.max_age_days, symbol=args.symbol)
