#!/usr/bin/env python3
"""
sam_gov_bot.py

Scans SAM.gov's public Opportunities API for "low hanging fruit" contract
opportunities: general supply/equipment PSC categories, set aside for small
business, in early/open notice stages (presolicitation, combined synopsis,
solicitation). Emails you a digest of anything new since the last run.

SETUP
-----
1. Get a SAM.gov API key:
   https://sam.gov -> sign in -> profile icon -> "Public API Key" -> Request API Key
   (Keys expire every 90 days -- SAM.gov will email you a reminder.)

2. Set the environment variables below (or edit the CONFIG section directly):

   export SAM_API_KEY="your_sam_gov_api_key"
   export SMTP_SERVER="smtp.gmail.com"
   export SMTP_PORT="587"
   export SMTP_USER="you@gmail.com"
   export SMTP_PASS="your_app_password"       # Gmail: use an App Password, not your real password
   export EMAIL_TO="you@gmail.com"

3. Run it once manually to test:
   python3 sam_gov_bot.py

4. Schedule it (e.g. daily at 7am) with cron:
   crontab -e
   0 7 * * * /usr/bin/python3 /path/to/sam_gov_bot.py >> /path/to/sam_gov_bot.log 2>&1

RATE LIMITS
-----------
Public API: 10 requests/day (unregistered use of the key beyond basic tier)
Registered/verified accounts: 1,000 requests/day
This script uses 1 request per PSC code per run (with pagination if needed),
so keep the PSC_CODES list reasonably small if you're on the 10/day tier.
"""

import os
import json
import time
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---------------------------------------------------------------------------
# CONFIG -- edit these to tune what counts as "low hanging fruit" for you
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("SAM_API_KEY", "PUT_YOUR_KEY_HERE")
BASE_URL = "https://api.sam.gov/prod/opportunities/v2/search"

# How many days back to look each run (keep this >= your run frequency so you
# never miss a posting even if a run fails).
LOOKBACK_DAYS = 3

# Notice types to include. Options include:
#   p = Presolicitation, k = Combined Synopsis/Solicitation, o = Solicitation,
#   r = Sources Sought, g = Sale of Surplus, a = Award Notice, i = Intent to Bundle
# We skip "a" (awards) since those are already decided -- not actionable.
NOTICE_TYPES = ["p", "k", "o"]

# PSC (Product/Service Classification) codes -- these are the FSC "supply
# group" prefixes for general supplies/equipment. Edit freely; SAM.gov's PSC
# manual has the full list if you want to narrow further:
# https://www.acquisition.gov/psc-manual
PSC_CODES = [
    "71",  # Furniture
    "72",  # Household & Commercial Furnishings/Appliances
    "73",  # Food Prep & Serving Equipment
    "75",  # Office Supplies & Devices
    "78",  # Recreational & Athletic Equipment
    "79",  # Cleaning Equipment & Supplies
    "80",  # Brushes, Paints, Sealers & Adhesives
    "84",  # Clothing, Textiles & Individual Equipment
    "87",  # Agricultural Supplies
]

# Small-business set-aside codes (this is what makes these "low hanging" --
# less competition from large primes). Full list of codes is in the SAM.gov
# API docs; these are the common ones.
SET_ASIDE_CODES = [
    "SBA",      # Total Small Business
    "SBP",      # Partial Small Business
    "8A",       # 8(a) Set-Aside
    "8AN",      # 8(a) Sole Source
    "HZC",      # HUBZone Set-Aside
    "HZS",      # HUBZone Sole Source
    "SDVOSBC",  # Service-Disabled Veteran-Owned Small Business Set-Aside
    "SDVOSBS",  # SDVOSB Sole Source
    "WOSB",     # Women-Owned Small Business
    "WOSBSS",   # WOSB Sole Source
    "EDWOSB",   # Economically Disadvantaged WOSB
    "EDWOSBSS", # EDWOSB Sole Source
]

RESULTS_PER_PAGE = 100  # API max per page is generally 1000, but keep modest
SEEN_FILE = "seen_notices.json"

# Email settings
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER)

# ---------------------------------------------------------------------------
# CORE LOGIC
# ---------------------------------------------------------------------------


def load_seen_ids():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(seen_ids):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen_ids), f, indent=2)


def fetch_opportunities_for_psc(psc_code, posted_from, posted_to):
    """Fetch all pages of opportunities for a single PSC code."""
    all_records = []
    offset = 0

    while True:
        params = {
            "api_key": API_KEY,
            "postedFrom": posted_from,
            "postedTo": posted_to,
            "limit": RESULTS_PER_PAGE,
            "offset": offset,
            "ptype": NOTICE_TYPES,
            "classificationCode": psc_code,
        }
        resp = requests.get(BASE_URL, params=params, timeout=30)

        if resp.status_code == 429:
            print(f"Rate limited on PSC {psc_code}. Stopping for this run.")
            break
        resp.raise_for_status()

        data = resp.json()
        records = data.get("opportunitiesData", [])
        all_records.extend(records)

        total = data.get("totalRecords", 0)
        offset += RESULTS_PER_PAGE
        if offset >= total or not records:
            break
        time.sleep(1)  # be polite between paged requests

    return all_records


def matches_set_aside(opp):
    return opp.get("typeOfSetAside") in SET_ASIDE_CODES


def run_scan():
    posted_to = datetime.utcnow()
    posted_from = posted_to - timedelta(days=LOOKBACK_DAYS)
    posted_from_str = posted_from.strftime("%m/%d/%Y")
    posted_to_str = posted_to.strftime("%m/%d/%Y")

    seen_ids = load_seen_ids()
    new_matches = []

    for psc in PSC_CODES:
        print(f"Querying PSC {psc} ({posted_from_str} - {posted_to_str})...")
        try:
            records = fetch_opportunities_for_psc(psc, posted_from_str, posted_to_str)
        except requests.HTTPError as e:
            print(f"  Error fetching PSC {psc}: {e}")
            continue

        for opp in records:
            notice_id = opp.get("noticeId")
            if not notice_id or notice_id in seen_ids:
                continue
            if not matches_set_aside(opp):
                continue

            new_matches.append(opp)
            seen_ids.add(notice_id)

        time.sleep(1)  # be polite between PSC codes

    save_seen_ids(seen_ids)
    return new_matches


def format_email_body(matches):
    if not matches:
        return None

    lines = [f"SAM.gov scan found {len(matches)} new low-hanging-fruit opportunity(ies):\n"]
    for opp in matches:
        title = opp.get("title", "Untitled")
        agency = opp.get("fullParentPathName") or opp.get("department", "Unknown agency")
        notice_type = opp.get("type", "Unknown type")
        set_aside = opp.get("typeOfSetAsideDescription") or opp.get("typeOfSetAside", "")
        posted = opp.get("postedDate", "")
        deadline = opp.get("responseDeadLine", "")
        notice_id = opp.get("noticeId", "")
        link = f"https://sam.gov/opp/{notice_id}/view" if notice_id else ""

        lines.append(
            f"- {title}\n"
            f"    Agency: {agency}\n"
            f"    Type: {notice_type} | Set-aside: {set_aside}\n"
            f"    Posted: {posted} | Response due: {deadline}\n"
            f"    Link: {link}\n"
        )

    return "\n".join(lines)


def send_email(body):
    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        print("Email not configured (missing SMTP_USER/SMTP_PASS/EMAIL_TO). Printing instead:\n")
        print(body)
        return

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"SAM.gov new opportunities - {datetime.utcnow().strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    print(f"Email sent to {EMAIL_TO}.")


def main():
    if API_KEY == "PUT_YOUR_KEY_HERE":
        raise SystemExit("Set SAM_API_KEY environment variable before running.")

    matches = run_scan()
    body = format_email_body(matches)

    if body:
        send_email(body)
    else:
        print("No new matching opportunities this run.")


if __name__ == "__main__":
    main()
