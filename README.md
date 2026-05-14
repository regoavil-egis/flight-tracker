# Daily flight-price tracker

Queries SerpAPI's Google Flights endpoint once a day for a configured set of routes and date windows, then posts a summary to a Slack channel and emails it via Gmail.

Runs in GitHub Actions — nothing on your machine needs to be on.

## What it does

- Daily at **9:00 AM Eastern (13:00 UTC)** the workflow fires.
- For each route × date window (currently 5 × 3 = **15 SerpAPI calls/day**), it asks Google Flights for round-trip options for 1 adult, economy, USD.
- It picks the cheapest option per route × window and posts a single summary message to Slack and an HTML email to Gmail.
- The raw JSON results are uploaded as a GitHub Actions artifact (30-day retention) so you can audit any day's run.

## One-time setup (~15 minutes)

### 1. Create a private GitHub repo

1. Go to https://github.com/new
2. Name it something like `flight-tracker`. Set visibility to **Private**.
3. Don't add a README or .gitignore — we have everything.

### 2. Upload these files to the repo

The repo should look like this:

```
flight-tracker/
├── flight_prices.py
├── config.json
├── requirements.txt
├── README.md  (this file — optional in the repo)
└── .github/
    └── workflows/
        └── daily-flights.yml
```

Easiest way: drag the entire `flight-tracker` folder onto the "uploading an existing file" page in your new GitHub repo, or use the command line:

```bash
cd flight-tracker
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin git@github.com:<your-username>/flight-tracker.git
git push -u origin main
```

### 3. Create a Slack incoming webhook

Slack incoming webhooks post to a *channel*, not a DM directly. The simplest approach is to create a private channel with just you in it.

1. In Slack, create a new private channel called `#flight-prices` (or whatever you'd like) and don't invite anyone else.
2. Go to https://api.slack.com/apps → **Create New App** → **From scratch** → name it "Flight Tracker", pick your workspace.
3. In the app, go to **Incoming Webhooks** → toggle **Activate Incoming Webhooks** on.
4. Click **Add New Webhook to Workspace**, pick the `#flight-prices` channel, and authorize.
5. Copy the webhook URL — it looks like `https://hooks.slack.com/services/T.../B.../...`.

### 4. Create a Gmail app password

GitHub Actions can't sign in with your normal Google password (2FA blocks it). You need an app password.

1. Make sure 2-Step Verification is on for your Google account: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords (you may need to sign in again).
3. Create a new app password — name it "Flight Tracker". Google will show you a 16-character password. Copy it (no spaces).
4. The password authorizes SMTP access for that account only. Treat it as a secret.

### 5. Add secrets to your GitHub repo

1. In your repo on GitHub, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** and add each of these:

| Name | Value |
|---|---|
| `SERPAPI_KEY` | Your SerpAPI key from https://serpapi.com (the one you've already generated). |
| `SLACK_WEBHOOK_URL` | The URL from step 3 above. |
| `GMAIL_USER` | Your Gmail address (e.g., `you@gmail.com`). |
| `GMAIL_APP_PASSWORD` | The 16-character app password from step 4 (no spaces). |
| `GMAIL_TO` | Where to send the email. Defaults to `GMAIL_USER` if you skip this. |

### 6. Test it

1. In your repo, go to **Actions** → "Daily flight prices" → **Run workflow** → **Run workflow** (use the manual trigger).
2. Watch the job run. It should take ~30 seconds. The job log will say things like `[slack] sent=True` and `[gmail] sent to ...`.
3. Check Slack and Gmail. You should see the day's summary.

If the workflow fails: open the failed run, expand the **Run flight tracker** step, and read the error. Common causes:
- `SERPAPI_KEY` not set → secret name typo.
- `[gmail] FAILED: SMTPAuthenticationError` → app password wrong, or 2FA not on for the Google account.
- `[slack] sent=False, HTTP 404` → webhook URL typo or the Slack app was disabled.

After the manual test succeeds, the cron will automatically run every day at 13:00 UTC (9 AM ET during EDT).

## Editing routes or date windows

Edit `config.json` in the repo, commit, push. Next run picks it up automatically. No code changes needed.

To add a new origin city:
```json
{"label": "Miami (MIA)", "departure_ids": ["MIA"]}
```

To rotate to a new set of trip windows after July, just update the `date_windows` array.

## Cost / rate limits

- **SerpAPI**: this config uses **15 searches/day ≈ 450/month**.
  - Free tier: 100/month → you'd burn through it in ~6 days.
  - Cheapest paid plan: $75/mo for 5,000 searches → plenty of headroom.
  - To stay closer to free, reduce to 1 date window per day or drop a route or two.
- **GitHub Actions**: free for public repos; private repos get 2,000 minutes/month free, this uses ~1 minute/day, so ~30 min/month — well under the cap.
- **Slack incoming webhooks**: free.
- **Gmail SMTP**: free for personal Gmail accounts up to ~500 emails/day.

## DST note

GitHub Actions cron always runs in UTC. `0 13 * * *` = 9:00 AM ET during **EDT** (~mid-March to early November) and 8:00 AM ET during **EST** (early November to mid-March). If you want exactly 9:00 AM year-round, edit the cron in `.github/workflows/daily-flights.yml`:
- EDT half of year: `0 13 * * *`
- EST half of year: `0 14 * * *`

Or just accept the 1-hour seasonal drift — most people don't care.

## How to stop it

Either:
- Disable the workflow: **Actions tab → "Daily flight prices" → "..." menu → Disable workflow**, or
- Delete the repo.

## Local testing

If you want to run it locally before committing:

```bash
export SERPAPI_KEY=...
python3 flight_prices.py --dry-run --limit 1   # 1 API call, prints to stdout
python3 flight_prices.py --dry-run             # all 15 calls, prints to stdout (no Slack/Gmail)
```

`--dry-run` skips Slack and Gmail delivery and just prints the summary.

## Files

- `flight_prices.py` — the agent. Standard library only.
- `config.json` — routes, windows, and search params. Edit this to change what's tracked.
- `requirements.txt` — empty (no third-party deps).
- `.github/workflows/daily-flights.yml` — schedules the daily run.
