"""Daily QQQ / 161-MA signal — computes today's action and optionally emails it.

The rule (no overheating; matches the CLI default):
  * QQQ close ABOVE its 161-day MA  -> DCA $X into TQQQ today.
  * QQQ close BELOW its 161-day MA  -> sell TQQQ, hold cash (SGOV).

The email headline is the % distance from the MA (positive = above). It reuses
``fetch_data`` (to refresh QQQ) and ``strategy.compute_signals`` (the exact same
signal the backtest uses), so the alert can never drift from the strategy.

Usage:
  python src/daily_signal.py --dry-run                 # print only, no email
  python src/daily_signal.py --to you@example.com      # compute + email (SMTP)
Env vars for SMTP email (e.g. Gmail app password):
  SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587),
  SMTP_USER, SMTP_PASS, MAIL_FROM (default = SMTP_USER)
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

import pandas as pd

from data import DATA_DIR, _load_ndx
from strategy import MA_WINDOW, compute_signals


@dataclass
class Signal:
    date: str
    qqq: float
    ma: float
    pct_from_ma: float      # (qqq/ma - 1) * 100
    above: bool
    crossed_today: bool     # regime differs from the prior trading day
    ma_window: int
    contribution: float

    @property
    def action(self) -> str:
        if self.above:
            verb = "CROSSED ABOVE today — RE-ENTER / start DCA" if self.crossed_today \
                else "ABOVE MA — keep DCA"
            return f"{verb}: buy ${self.contribution:,.0f} TQQQ"
        verb = "CROSSED BELOW today — SELL TQQQ now" if self.crossed_today \
            else "BELOW MA — stay in cash"
        return f"{verb}: hold SGOV/cash, no TQQQ"

    def subject(self) -> str:
        side = "ABOVE" if self.above else "BELOW"
        return (f"TQQQ {self.ma_window}MA — {side} ({self.pct_from_ma:+.1f}%) "
                f"— {self.date}")

    def body(self) -> str:
        lines = [
            f"Date:            {self.date}",
            f"QQQ close:       {self.qqq:,.2f}",
            f"{self.ma_window}-day MA:      {self.ma:,.2f}",
            f"Distance from MA:{self.pct_from_ma:+.2f}%   <-- headline",
            "",
            f"Signal:          QQQ is {'ABOVE' if self.above else 'BELOW'} its "
            f"{self.ma_window}-day MA"
            + ("  (regime CHANGED today)" if self.crossed_today else ""),
            f"Action:          {self.action}",
            "",
            "Rule: above the MA -> DCA into TQQQ; below -> sell TQQQ to cash.",
            "Informational only — not a trade order or financial advice.",
        ]
        return "\n".join(lines)


def compute_today(
    contribution: float = 100.0,
    ma_window: int = MA_WINDOW,
    auto_update: bool = True,
) -> Signal:
    """Refresh QQQ, compute the latest confirmed 161-MA signal."""
    ndx_path = DATA_DIR / "NDX.csv"
    if auto_update:
        from fetch_data import ensure_ndx_csv
        ensure_ndx_csv(ndx_path, auto_update=True)

    ndx = _load_ndx(ndx_path)  # real Nasdaq-100 index level

    # Compute the signal directly on the index. The above/below relationship and
    # the % distance from the MA are scale-invariant, so using the raw index
    # (rather than the $100 proxy) lets us show real, recognisable levels.
    # No overheating (threshold=None): matches the CLI default.
    sig = compute_signals(ndx, ma_window=ma_window, overheated_threshold=None)
    f = sig.frame
    if len(f) < 2:
        raise SystemExit("Not enough data to compute a signal.")

    last = f.iloc[-1]
    prev = f.iloc[-2]
    pct = float(last["qqq"] / last["ma"] - 1.0) * 100.0  # 'qqq' col == the input
    return Signal(
        date=f.index[-1].strftime("%Y-%m-%d"),
        qqq=float(last["qqq"]),
        ma=float(last["ma"]),
        pct_from_ma=pct,
        above=bool(last["above"]),
        crossed_today=bool(last["above"] != prev["above"]),
        ma_window=ma_window,
        contribution=contribution,
    )


def send_email(subject: str, body: str, to_addr: str) -> None:
    """Send via SMTP using env-var credentials (e.g. a Gmail app password)."""
    import smtplib
    from email.message import EmailMessage

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]        # required
    pw = os.environ["SMTP_PASS"]          # required (app password)
    sender = os.environ.get("MAIL_FROM", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg.set_content(body)

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)


def main() -> None:
    p = argparse.ArgumentParser(description="Daily QQQ 161-MA signal + email")
    p.add_argument("--to", help="recipient email address")
    p.add_argument("--contribution", type=float, default=100.0,
                   help="DCA amount to suggest when above the MA (default 100)")
    p.add_argument("--ma", type=int, default=MA_WINDOW)
    p.add_argument("--dry-run", action="store_true", help="print only, no email")
    p.add_argument("--no-update", action="store_true",
                   help="use cached data, do not fetch")
    args = p.parse_args()

    sig = compute_today(
        contribution=args.contribution, ma_window=args.ma,
        auto_update=not args.no_update,
    )

    print(sig.subject())
    print("-" * len(sig.subject()))
    print(sig.body())

    if not args.dry_run:
        if not args.to:
            raise SystemExit("Provide --to <email> (or use --dry-run).")
        send_email(sig.subject(), sig.body(), args.to)
        print(f"\n[sent to {args.to}]")


if __name__ == "__main__":
    main()
