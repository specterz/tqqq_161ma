"""Daily QQQ / 161-MA signal — computes today's action and optionally emails it.

The rule (no overheating; matches the CLI default):
  * QQQ close ABOVE its 161-day MA  -> DCA $X into TQQQ today.
  * QQQ close BELOW its 161-day MA  -> sell TQQQ, hold cash (SGOV).

The email headline is the % distance from the MA (positive = above). It reuses
``fetch_data`` (to refresh QQQ) and ``strategy.compute_signals`` (the exact same
signal the backtest uses), so the alert can never drift from the strategy.

Usage:
  python src/daily_signal.py --dry-run                    # print only, no send
  python src/daily_signal.py --discord https://discord... # post to Discord
  python src/daily_signal.py --discord                    # use DISCORD_WEBHOOK env
  python src/daily_signal.py --to you@example.com         # email via SMTP
  # (combine --discord and --to to send both)

Delivery options:
  * Discord webhook (simplest): create one in Discord under Server Settings ->
    Integrations -> Webhooks. Pass the URL via --discord or DISCORD_WEBHOOK.
  * SMTP email (e.g. Gmail app password): SMTP_HOST (default smtp.gmail.com),
    SMTP_PORT (default 587), SMTP_USER, SMTP_PASS, MAIL_FROM (default SMTP_USER).
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
    qqq: float              # NDX close (the signal source; QQQ tracks it 1:1)
    ma: float
    pct_from_ma: float      # (ndx/ma - 1) * 100 — same % as QQQ vs its MA
    above: bool
    crossed_today: bool     # regime differs from the prior trading day
    ma_window: int
    contribution: float

    @property
    def action(self) -> str:
        # Four phrasings from two booleans: above/below the MA x whether the
        # regime flipped today. A fresh cross gets an urgent verb (RE-ENTER/SELL
        # NOW); an unchanged regime just restates the standing instruction.
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
            f"NDX close:       {self.qqq:,.2f}",
            f"{self.ma_window}-day MA:      {self.ma:,.2f}",
            f"Distance from MA:{self.pct_from_ma:+.2f}%   <-- headline",
            "",
            f"Signal:          NDX is {'ABOVE' if self.above else 'BELOW'} its "
            f"{self.ma_window}-day MA"
            + ("  (regime CHANGED today)" if self.crossed_today else ""),
            f"Action:          {self.action}",
            "",
            "Signal source: Nasdaq-100 index (NDX); QQQ tracks it, so the % "
            "distance is the same. Rule: above the MA -> DCA into TQQQ; "
            "below -> sell TQQQ to cash.",
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

    # Latest row is today's confirmed signal; the row before it lets us detect a
    # regime flip (a "crossed today" cross of the MA).
    last = f.iloc[-1]
    prev = f.iloc[-2]
    pct = float(last["qqq"] / last["ma"] - 1.0) * 100.0  # 'qqq' col == the input
    return Signal(
        date=f.index[-1].strftime("%Y-%m-%d"),
        qqq=float(last["qqq"]),
        ma=float(last["ma"]),
        pct_from_ma=pct,
        above=bool(last["above"]),
        # Regime changed iff today's above-flag differs from yesterday's.
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
        s.starttls()          # upgrade to TLS before sending credentials (587 is submission)
        s.login(user, pw)
        s.send_message(msg)


def post_discord(sig: "Signal", webhook_url: str) -> None:
    """POST the signal to a Discord incoming webhook (stdlib only).

    Uses a compact rich "embed": green when above the MA, red when below, with
    the % distance as the headline field. Create the webhook in Discord under
    Server Settings -> Integrations -> Webhooks -> New Webhook -> Copy URL.
    """
    import json
    import urllib.error
    import urllib.request

    # Normalise the legacy domain: discordapp.com issues a cross-host redirect
    # that urllib won't follow on a POST (surfaces as 403). discord.com is direct.
    webhook_url = webhook_url.replace("discordapp.com", "discord.com")

    color = 0x3FB950 if sig.above else 0xF85149  # green above, red below
    payload = {
        "username": "161MA Signal",
        "embeds": [
            {
                "title": sig.subject(),
                "color": color,
                "fields": [
                    {"name": "Distance from MA",
                     "value": f"**{sig.pct_from_ma:+.2f}%**", "inline": True},
                    {"name": "NDX close",
                     "value": f"{sig.qqq:,.2f}", "inline": True},
                    {"name": f"{sig.ma_window}-day MA",
                     "value": f"{sig.ma:,.2f}", "inline": True},
                    {"name": "Action", "value": sig.action, "inline": False},
                ],
                "footer": {"text": "Informational only — not a trade order."},
            }
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={
            "Content-Type": "application/json",
            # Discord (via Cloudflare) rejects urllib's default UA with 403.
            # A browser-like User-Agent + Accept reliably passes the edge check.
            "User-Agent": "Mozilla/5.0 (compatible; tqqq-161ma-signal/1.0; "
                          "+https://github.com/specterz/tqqq_161ma)",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Discord webhook returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        # Surface Discord's error body (e.g. bad URL, rate limit) for debugging.
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Discord webhook HTTP {exc.code}: {detail}") from exc


def main() -> None:
    p = argparse.ArgumentParser(description="Daily QQQ 161-MA signal alert")
    p.add_argument("--to", help="email recipient (SMTP; needs SMTP_* env vars)")
    p.add_argument("--discord", nargs="?", const="", default=None,
                   help="Discord webhook URL (or omit the value to read the "
                        "DISCORD_WEBHOOK env var)")
    p.add_argument("--contribution", type=float, default=100.0,
                   help="DCA amount to suggest when above the MA (default 100)")
    p.add_argument("--ma", type=int, default=MA_WINDOW)
    p.add_argument("--dry-run", action="store_true", help="print only, no send")
    p.add_argument("--no-update", action="store_true",
                   help="use cached data, do not fetch")
    p.add_argument("--once-state", metavar="FILE",
                   help="dedupe file: skip sending if it already records the "
                        "current signal date (prevents duplicate alerts when "
                        "GitHub cron double-fires). Updated after a successful send.")
    args = p.parse_args()

    sig = compute_today(
        contribution=args.contribution, ma_window=args.ma,
        auto_update=not args.no_update,
    )

    print(sig.subject())
    print("-" * len(sig.subject()))
    print(sig.body())

    if args.dry_run:
        return

    # Dedupe guard: if we've already alerted for this signal date, skip. This
    # makes a double-fired / delayed-then-retried schedule harmless — the second
    # run sees the date already recorded and exits without posting again.
    if args.once_state:
        from pathlib import Path
        state_path = Path(args.once_state)
        last = state_path.read_text().strip() if state_path.exists() else ""
        if last == sig.date:
            print(f"\n[skip] already alerted for {sig.date}; not sending again.")
            return

    sent = False   # track whether any channel actually delivered
    # Discord: --discord <url>, or --discord (bare) to use DISCORD_WEBHOOK env.
    # args.discord is None only when the flag was omitted entirely (const=""
    # makes the bare flag an empty string, which then falls back to the env var).
    if args.discord is not None:
        url = args.discord or os.environ.get("DISCORD_WEBHOOK", "")
        if not url:
            raise SystemExit("Discord selected but no URL (pass it or set "
                             "DISCORD_WEBHOOK).")
        post_discord(sig, url)
        print("\n[posted to Discord]")
        sent = True

    if args.to:
        send_email(sig.subject(), sig.body(), args.to)
        print(f"\n[emailed to {args.to}]")
        sent = True

    if not sent:
        raise SystemExit("Nothing sent. Use --discord and/or --to, or --dry-run.")

    # Record the date we just alerted for, so a later same-day run skips (above).
    if args.once_state:
        from pathlib import Path
        Path(args.once_state).write_text(sig.date + "\n")


if __name__ == "__main__":
    main()
