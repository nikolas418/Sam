# SAM.gov Opportunity Scanner

Scans SAM.gov daily for small-business set-aside opportunities in general
supply/equipment PSC categories, and emails a digest of anything new.

Runs automatically every day via GitHub Actions -- no computer needs to be on.

## One-time setup

### 1. Get a SAM.gov API key
- Sign in at https://sam.gov
- Profile icon (top right) -> "Public API Key" -> Request API Key
- Copy the key somewhere safe (it expires every 90 days; SAM.gov emails a
  reminder beforehand -- you'll need to update the GitHub secret when it
  rotates)

### 2. Set up a Gmail App Password (or use another SMTP provider)
- Turn on 2-factor authentication on your Google account
- Go to Google Account -> Security -> App Passwords
- Generate one for "Mail" -- this is what goes in `SMTP_PASS`, not your
  real Gmail password

### 3. Add secrets to this GitHub repo
In this repo: **Settings -> Secrets and variables -> Actions -> New repository secret**

Add each of these:

| Secret name    | Value                                      |
|-----------------|--------------------------------------------|
| `SAM_API_KEY`   | your SAM.gov API key                       |
| `SMTP_SERVER`   | `smtp.gmail.com` (or your provider's SMTP)  |
| `SMTP_PORT`     | `587`                                       |
| `SMTP_USER`     | your email address                         |
| `SMTP_PASS`     | your app password                          |
| `EMAIL_TO`      | where you want the digest sent             |

### 4. Test it manually
Go to the **Actions** tab in this repo -> "SAM.gov Opportunity Scan" ->
"Run workflow" (this is the `workflow_dispatch` trigger). Watch the run logs
to confirm it completes and check your email.

### 5. Let it run
Once the manual test works, it'll run automatically every day at the time
set in `.github/workflows/sam-scan.yml` (default: 12:00 UTC). No further
action needed -- GitHub handles the scheduling.

## Tuning what counts as "low hanging fruit"

Edit `sam_gov_bot.py` directly and commit the change:
- `PSC_CODES` -- which product/service categories to search
- `SET_ASIDE_CODES` -- which set-aside types count
- `NOTICE_TYPES` -- which notice stages to include
- `LOOKBACK_DAYS` -- how many days back each scan checks (keep >= how often
  the job runs, so nothing slips through a missed run)

## Notes

- GitHub Actions free tier gives private repos 2,000 minutes/month; this job
  takes well under a minute per run, so you won't come close to any limit.
- The workflow commits `seen_notices.json` back to the repo after each run
  so de-duplication persists across runs (the runner itself is thrown away
  every time).
- If a run ever fails (e.g. SAM.gov API key expired), GitHub will email the
  repo owner automatically -- check the Actions tab for the error.
