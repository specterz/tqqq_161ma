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
python src/daily_signal.py --dry-run            # prints, no send
python src/daily_signal.py --dry-run --contribution 50
python src/daily_signal.py --discord "https://discord.com/api/webhooks/..."  # test post
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

## Automate the daily alert (GitHub Actions + Discord, free)

The workflow `.github/workflows/daily-signal.yml` runs every weekday ~21:30 UTC
(after the US close) and posts the signal to a Discord channel. To enable it:

1. **Create a Discord webhook:** in your Discord server, Server Settings →
   Integrations → Webhooks → New Webhook → pick a channel → **Copy Webhook URL**.
2. **Make the repo public** (or ensure you have Actions minutes) and push.
3. Add one repo **Secret**: Settings → Secrets and variables → Actions → New
   repository secret → name `DISCORD_WEBHOOK`, value = the webhook URL.
4. Enable Actions if prompted (Actions tab).
5. Test it now: Actions tab → "Daily 161-MA signal" → **Run workflow**.
6. Check your Discord channel — a green (above MA) or red (below MA) card with
   the % distance and action appears. After that it runs automatically each
   weekday, pushing a phone notification.

That's the whole setup — **one secret, no email server, no app password.**

### Prefer email instead (or as well)?

`daily_signal.py` also supports SMTP email via `--to you@example.com` with
`SMTP_USER` / `SMTP_PASS` (a Gmail **App Password**, created under Google Account
→ Security → App passwords) and optional `SMTP_HOST` / `SMTP_PORT` / `MAIL_FROM`.
You can pass both `--discord` and `--to` to send to both.

## Notes

- **Timing:** the alert reflects *today's close*, so it lands after market
  close — you act the next session. That matches how the strategy is defined
  (act on the confirmed daily close).
- **Data source:** the job refreshes the Nasdaq-100 index via Yahoo, emails the
  signal computed on that fresh data, then **commits the updated `data/NDX.csv`
  back to the repo** so it stays daily-fresh (also keeping the website snapshot
  current). The commit is skipped on days with no new bar (weekends/holidays),
  and carries `[skip ci]` so it never triggers other workflows.
- **Permissions:** the workflow needs `contents: write` (already set in the
  YAML). If the push is rejected, check Settings → Actions → General →
  "Workflow permissions" is set to **Read and write permissions**.
- **Not advice:** this is an informational alert. You place any actual trades.
