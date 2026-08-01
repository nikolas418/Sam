import os
import sys
import json
import re
from datetime import datetime, timedelta, timezone
import requests
from dateutil import parser as date_parser
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---------------------------------------------------------------------------
# CONFIGURATION & LOOSENED PARAMETERS
# ---------------------------------------------------------------------------
SAM_API_KEY = os.getenv("SAM_API_KEY")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")

SEEN_NOTICES_FILE = "seen_notices.json"

# LOOSENED THRESHOLDS (Score >= 1 qualifies for review)
STRONG_SCORE_THRESHOLD = 3
CONDITIONAL_SCORE_THRESHOLD = 1

# LOOSENED DEADLINE (Accept items due in > 4 hours)
MIN_HOURS_UNTIL_DEADLINE = 4

# STRICTLY MINIMIZED HARD EXCLUSIONS (Only drop extreme non-matches)
HARD_EXCLUDE_PATTERNS = [
    r"\bjanitorial\b",
    r"\bgrounds maintenance\b",
    r"\breal estate lease\b",
    r"\btrash removal\b",
]

# POSITIVE SCORING KEYWORDS
POSITIVE_KEYWORDS = [
    "purchase", "procurement", "supply", "delivery", "equipment", "cots",
    "hardware", "software", "components", "replacement", "system", "parts",
    "rfq", "solicitation", "brand name", "commercial", "item", "unit", "mod",
    "battery", "cable", "adapter", "tool", "device"
]

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def load_seen_notices():
    if os.path.exists(SEEN_NOTICES_FILE):
        try:
            with open(SEEN_NOTICES_FILE, "r") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Warning: Could not load seen notices file: {e}")
    return set()

def save_seen_notices(seen_set):
    try:
        with open(SEEN_NOTICES_FILE, "w") as f:
            json.dump(list(seen_set), f, indent=2)
    except Exception as e:
        print(f"Error saving seen notices: {e}")

def parse_deadline(notice):
    deadline_str = notice.get("responseDeadLine") or notice.get("archiveDate")
    if not deadline_str:
        return None
    try:
        dt = date_parser.parse(deadline_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def fetch_sam_notices(days_back=7):
    if not SAM_API_KEY:
        print("ERROR: SAM_API_KEY environment variable is missing.")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=days_back)).strftime("%m/%d/%Y")
    to_date = now.strftime("%m/%d/%Y")

    print(f"Scanning SAM.gov ({from_date} to {to_date})...")

    url = "https://api.sam.gov/prod/opportunities/v2/search"
    params = {
        "api_key": SAM_API_KEY,
        "postedFrom": from_date,
        "postedTo": to_date,
        "limit": 500,  # Fetch up to 500 opportunities
        "ptype": "o,k,p,r"  # Solicitation, Combined Synopsis, Presolicitation, Sources Sought
    }

    try:
        response = requests.get(url, params=params, timeout=45)
        response.raise_for_status()
        data = response.json()
        records = data.get("opportunitiesData", [])
        print(f"Fetched {len(records)} raw record(s) from SAM.gov.")
        return records
    except Exception as e:
        print(f"ERROR fetching data from SAM.gov: {e}")
        return []

def evaluate_notice(notice, now_utc):
    title = notice.get("title", "No Title").strip()
    desc = notice.get("description", "") or ""

    # 1. Deadline Check
    deadline_dt = parse_deadline(notice)
    if deadline_dt:
        hours_left = (deadline_dt - now_utc).total_seconds() / 3600.0
        if hours_left < MIN_HOURS_UNTIL_DEADLINE:
            return "REJECTED", 0, f"Due too soon ({hours_left:.1f} hrs left)"

    # 2. Hard Exclusion Check
    full_text = f"{title} {desc}".lower()
    for pattern in HARD_EXCLUDE_PATTERNS:
        if re.search(pattern, full_text):
            return "REJECTED", 0, f"Matched hard exclusion pattern: '{pattern}'"

    # 3. Scoring Engine
    score = 1  # Base score for passing initial filters

    # Score bonus for positive keywords in title/description
    matched_words = []
    for kw in POSITIVE_KEYWORDS:
        if kw in full_text:
            score += 1
            matched_words.append(kw)

    # Classification logic
    if score >= STRONG_SCORE_THRESHOLD:
        return "STRONG", score, f"Passed with keywords: {', '.join(matched_words[:4])}"
    elif score >= CONDITIONAL_SCORE_THRESHOLD:
        return "CONDITIONAL", score, f"Passed with score {score}"
    else:
        return "REJECTED", score, f"Score {score} below threshold {CONDITIONAL_SCORE_THRESHOLD}"

# ---------------------------------------------------------------------------
# EMAIL & REPORTING
# ---------------------------------------------------------------------------
def send_email_digest(strong_matches, conditional_matches):
    all_notices_to_report = strong_matches + conditional_matches
    if not all_notices_to_report:
        print("No qualified notices to report.")
        return

    if not all([SMTP_SERVER, SMTP_USER, SMTP_PASS, EMAIL_TO]):
        print("SMTP environment variables missing. Skipping email send.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"SAM.gov Opportunity Alert: {len(all_notices_to_report)} Qualified Items"
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO

    html_lines = [
        "<h2>SAM.gov Opportunity Scan Results</h2>",
        f"<p><strong>Total Approved Opportunities:</strong> {len(all_notices_to_report)}</p>",
        "<hr/>"
    ]

    for category_name, items in [("Strong Matches", strong_matches), ("Conditional Matches", conditional_matches)]:
        if not items:
            continue
        html_lines.append(f"<h3>{category_name} ({len(items)})</h3><ul>")
        for item in items:
            notice = item["notice"]
            sol_num = notice.get("solicitationNumber") or notice.get("noticeId") or "N/A"
            title = notice.get("title", "No Title")
            link = notice.get("uiLink", "#")
            posted = notice.get("postedDate", "N/A")
            score = item["score"]
            reason = item["reason"]

            html_lines.append(
                f"<li>"
                f"<strong>[{sol_num}]</strong> <a href='{link}'>{title}</a><br/>"
                f"<strong>Score:</strong> {score} | <strong>Posted:</strong> {posted}<br/>"
                f"<em>Notes:</em> {reason}"
                f"</li><br/>"
            )
        html_lines.append("</ul>")

    msg.attach(MIMEText("\n".join(html_lines), "html"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        print(f"Scan digest email sent to {EMAIL_TO}.")
    except Exception as e:
        print(f"Failed to send email: {e}")

# ---------------------------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------------------------
def main():
    seen_notices = load_seen_notices()
    raw_notices = fetch_sam_notices(days_back=7)
    now_utc = datetime.now(timezone.utc)

    strong_matches = []
    conditional_matches = []
    rejected_count = 0

    print("\n--- PROCESSING NOTICES ---")
    for notice in raw_notices:
        notice_id = notice.get("noticeId") or notice.get("solicitationNumber")
        title = notice.get("title", "No Title").strip()

        if notice_id and notice_id in seen_notices:
            continue  # Skip already processed items

        status, score, reason = evaluate_notice(notice, now_utc)

        if status == "STRONG":
            print(f"[STRONG MATCH] Score: {score} | {notice_id} | {title[:60]}")
            strong_matches.append({"notice": notice, "score": score, "reason": reason})
            if notice_id:
                seen_notices.add(notice_id)
        elif status == "CONDITIONAL":
            print(f"[CONDITIONAL MATCH] Score: {score} | {notice_id} | {title[:60]}")
            conditional_matches.append({"notice": notice, "score": score, "reason": reason})
            if notice_id:
                seen_notices.add(notice_id)
        else:
            rejected_count += 1
            print(f"[REJECTED] {notice_id} | {title[:50]} | Reason: {reason}")

    print("--------------------------------------------------")
    print(f"Scan Summary: {len(strong_matches)} Strong, {len(conditional_matches)} Conditional, {rejected_count} Rejected.")

    save_seen_notices(seen_notices)
    send_email_digest(strong_matches, conditional_matches)

if __name__ == "__main__":
    main()
