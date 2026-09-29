# Daily 161-MA signal email

Get a once-a-day email telling you the strategy's action, without visiting any
website. It reuses the exact backtest signal (`strategy.compute_signals`), so
the alert can never drift from the strategy.

## The rule (no overheating)

- **QQQ closes ABOVE its 161-day MA** → DCA $X into TQQQ today.
- **QQQ closes BELOW its 161-day MA** → sell TQQQ, hold cash (SGOV).

The email headline is the **% distance from the 161-MA** (positive = above).
Cross days ("crossed BELOW today — SELL") are called out explicitly.

## Try it locally first

```bash
python src/daily_signal.py --dry-run            # prints, no email
python src/daily_signal.py --dry-run --contribution 50
```

Example output:

```
TQQQ 161MA — ABOVE (+9.6%) — 2026-09-25
Date:            2026-09-25
QQQ close:       30,466.48
161-day MA:      27,805.76
Distance from MA:+9.57%   <-- headline
Action:          ABOVE MA — keep DCA: buy $50 TQQQ
```

## Automate the daily email (GitHub Actions, free)

The workflow `.github/workflows/daily-signal.yml` runs every weekday ~21:30 UTC
(after the US close) and emails you. To enable it:

1. **Make the repo public** (or ensure you have Actions minutes) and push.
2. Add repo **Secrets**: Settings → Secrets and variables → Actions → New
   repository secret. Add:
   - `SMTP_USER` — your sending email (e.g. a Gmail address)
   - `SMTP_PASS` — an **app password**, not your normal password (see below)
   - `MAIL_TO`   — where the alert goes (can be the same address)
   - *(optional)* `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`
3. Enable Actions if prompted (Actions tab).
4. Test it now: Actions tab → "Daily 161-MA signal email" → **Run workflow**.
5. Check your inbox. After that it runs automatically each weekday.

### Gmail app password

Normal Gmail passwords won't work over SMTP. Turn on 2-Step Verification, then
create an **App Password** (Google Account → Security → App passwords) and use
that 16-character value as `SMTP_PASS`. Any SMTP provider works; adjust
`SMTP_HOST`/`SMTP_PORT` accordingly.

## Notes

- **Timing:** the alert reflects *today's close*, so it lands after market
  close — you act the next session. That matches how the strategy is defined
  (act on the confirmed daily close).
- **Data source:** the job refreshes the Nasdaq-100 index via Yahoo. If a fetch
  fails the run errors (visible in the Actions tab) rather than emailing a stale
  signal.
- **Not advice:** this is an informational alert. You place any actual trades.
