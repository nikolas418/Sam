#!/usr/bin/env python3
"""
sam_gov_bot_corrected_psc.py

Scans SAM.gov's public Opportunities API for "low hanging fruit" contract
opportunities: equipment/supply-heavy Product Service Code (PSC) categories,
small-business set-asides, active/open notice stages, and enough time left to
respond. Emails a PDF digest of anything new since the last run.

Key corrections in this version:
- Uses the current SAM.gov Opportunities v2 endpoint.
- Uses "ccode" for Product Service Code / classification-code filtering.
- Adds PSC 3695: Miscellaneous Special Industry Machinery.
- Keeps the small-business set-aside filter.
- Fixes undefined variable errors from SAM_API_KEY / psc_code / notice_type.
- Keeps a client-side PSC and set-aside safety check after API results return.
- Uses matching limit and offset values so records are not skipped.

SETUP
-----
1. Get a SAM.gov API key:
   https://sam.gov -> sign in -> profile icon -> Public API Key

2. Set environment variables:

   export SAM_API_KEY="your_sam_gov_api_key"
   export SMTP_SERVER="smtp.gmail.com"
   export SMTP_PORT="587"
   export SMTP_USER="you@gmail.com"
   export SMTP_PASS="your_app_password"       # Gmail: use an App Password
   export EMAIL_TO="you@gmail.com"

3. Run it once manually:

   python3 sam_gov_bot_corrected_psc.py

4. Schedule it with cron, for example daily at 7am:

   crontab -e
   0 7 * * * /usr/bin/python3 /path/to/sam_gov_bot_corrected_psc.py >> /path/to/sam_gov_bot.log 2>&1

DEPENDENCIES
------------
pip install requests python-dateutil reportlab
"""

import csv
import json
import os
import re
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from xml.sax.saxutils import escape

import requests
from dateutil import parser as date_parser
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer
from reportlab.lib.styles import getSampleStyleSheet


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("SAM_API_KEY", "PUT_YOUR_KEY_HERE")
BASE_URL = "https://api.sam.gov/opportunities/v2/search"

# How many days back to look each run.
# Keep this >= run frequency so you do not miss a posting if a run fails.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))

# Skip anything due too soon to realistically quote.
# Opportunities with no listed deadline are kept.
MIN_HOURS_UNTIL_DEADLINE = int(os.environ.get("MIN_HOURS_UNTIL_DEADLINE", "36"))

# Notice types:
# p = Presolicitation
# k = Combined Synopsis/Solicitation
# o = Solicitation
# We skip awards because they are already decided.
NOTICE_TYPES = ["p", "k", "o"]

# PSC / Product Service Code filters.
# SAM.gov request parameter is "ccode".
# SAM.gov response field is "classificationCode".
PSC_CODES = [
    "34",    # Metalworking machinery
    "35",    # Service and trade equipment
    "36",    # Special industry machinery - broad bucket
    "3695",  # Miscellaneous special industry machinery
    "39",    # Materials handling equipment
    "41",    # Refrigeration / AC equipment
    "63",    # Security / detection systems
    "65",    # Medical / dental / vet equipment
    "66",    # Instruments / lab equipment
    "67",    # Photographic / video equipment
    "71",    # Furniture
    "72",    # Household / commercial furnishings
    "73",    # Food prep / serving equipment
    "74",    # Office machines / business equipment
]

# Optional expanded equipment PSCs.
# Turn on with: export EXPANDED_EQUIPMENT_PSC="1"
EXPANDED_PSC_CODES = [
    "23",  # Ground vehicles / trailers
    "24",  # Tractors
    "37",  # Agricultural machinery and equipment
    "38",  # Construction / mining / excavating equipment
    "42",  # Firefighting / rescue / safety equipment
    "51",  # Hand tools
    "52",  # Measuring tools
    "54",  # Prefabricated structures / scaffolding
    "70",  # ADP equipment / software / supplies
]

if os.environ.get("EXPANDED_EQUIPMENT_PSC", "0") == "1":
    PSC_CODES.extend(EXPANDED_PSC_CODES)

# PSCs to avoid because they usually pull services, parts, repairs, or construction.
AVOID_PSC_CODES = [
    "J",   # Maintenance / repair / rebuild of equipment
    "R",   # Professional services
    "S",   # Utilities / housekeeping / grounds services
    "Y",   # Construction
    "Z",   # Maintenance / repair of real property
    "53",  # Hardware / fasteners
    "59",  # Electrical components
    "61",  # Electric wire / power distribution equipment
    "47",  # Pipe / tubing / hose
    "28",  # Engines / turbines / components
    "29",  # Engine accessories
]

# Small-business set-aside codes.
# We filter these client-side to avoid multiplying API calls by every set-aside code.
SET_ASIDE_CODES = [
    "SBA",       # Total Small Business
    "SBP",       # Partial Small Business
    "8A",        # 8(a) Set-Aside
    "8AN",       # 8(a) Sole Source
    "HZC",       # HUBZone Set-Aside
    "HZS",       # HUBZone Sole Source
    "SDVOSBC",   # SDVOSB Set-Aside
    "SDVOSBS",   # SDVOSB Sole Source
    "WOSB",      # Women-Owned Small Business
    "WOSBSS",    # WOSB Sole Source
    "EDWOSB",    # Economically Disadvantaged WOSB
    "EDWOSBSS",  # EDWOSB Sole Source
]

# Title keywords that usually signal service/repair/install/construction work.
# We still scan equipment PSCs, but reject obvious service-heavy titles.
EXCLUDE_TITLE_KEYWORDS = [
    "REPAIR",
    "MODIFICATION",
    "MAINTENANCE",
    "MAINT ",
    "INSTALL",
    "INSTALLATION",
    "RENOVATE",
    "RENOVATION",
    "REHAB",
    "OVERHAUL",
    "DEMOLITION",
    "SERVICE",
    "SERVICES",
    "SUPPORT SERVICES",
    "TRAINING",
    "LODGING",
    "RENTAL",
    "CONSTRUCTION",
    "ABATEMENT",
    "MITIGATION",
    "COMPLIANCE",
    "ADVISORY",
    "WASHING",
    "DISPOSAL",
    "INSPECTION",
    "TESTING",
    "CARPET CLEANING",
    "WINDOW CLEANING",
    "DUCT CLEANING",
    "CLEANING SERVICE",
    "PEST CONTROL",
    "MOWING",
    "VEGETATION",
]

# Extra rejection for DLA-style commodity parts. These can be bid sometimes,
# but they are usually not the easy supply/equipment wins we are looking for.
DLA_PART_TITLE_PATTERNS = [
    r"^\s*\d{2}\s*[-–—]",          # e.g., "53--BOLT" or "59 - CONNECTOR"
    r"\bNSN\b",                    # National Stock Number language
    r"\bNOUN\b",
    r"\bSPARE PARTS?\b",
    r"\bREPAIR PARTS?\b",
    r"\bPARTS KIT\b",
]

# API safety controls.
MAX_PAGES_PER_QUERY = int(os.environ.get("MAX_PAGES_PER_QUERY", "10"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "45"))
RESULTS_PER_PAGE = int(os.environ.get("RESULTS_PER_PAGE", "1000"))
MAX_API_CALLS_PER_RUN = int(os.environ.get("MAX_API_CALLS_PER_RUN", "250"))

# Local output files live beside the script so cron runs from any directory work.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.environ.get("SEEN_FILE", os.path.join(SCRIPT_DIR, "seen_notices.json"))
PDF_PATH = os.environ.get("PDF_PATH", os.path.join(SCRIPT_DIR, "sam_gov_opportunities.pdf"))
CSV_PATH = os.environ.get("CSV_PATH", os.path.join(SCRIPT_DIR, "sam_gov_opportunities.csv"))

# Email settings.
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------


def load_seen_ids():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen_ids(seen_ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_ids), f, indent=2)


def clean_text(value):
    return str(value or "").strip()


def get_set_aside_code(opp):
    """SAM responses can expose set-aside data under more than one field."""
    return clean_text(
        opp.get("typeOfSetAside")
        or opp.get("setAsideCode")
        or ""
    ).upper()


def get_set_aside_description(opp):
    return clean_text(
        opp.get("typeOfSetAsideDescription")
        or opp.get("setAside")
        or get_set_aside_code(opp)
    )


def get_response_deadline(opp):
    """Support both observed and documented misspelled response-deadline keys."""
    return clean_text(
        opp.get("responseDeadLine")
        or opp.get("responseDeadline")
        or opp.get("reponseDeadLine")
    )


def get_notice_link(opp):
    notice_id = clean_text(opp.get("noticeId"))
    ui_link = clean_text(opp.get("uiLink"))

    if ui_link and ui_link.lower() not in {"null", "none"}:
        return ui_link
    if notice_id:
        return f"https://sam.gov/opp/{notice_id}/view"
    return ""


def get_poc_summary(opp):
    contacts = opp.get("pointOfContact") or opp.get("pointofContact") or []
    if not isinstance(contacts, list):
        return ""

    pieces = []
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        name = clean_text(contact.get("fullName") or contact.get("fullname"))
        email = clean_text(contact.get("email"))
        phone = clean_text(contact.get("phone"))
        parts = [part for part in [name, email, phone] if part]
        if parts:
            pieces.append(" / ".join(parts))

    return "; ".join(pieces)


def matches_set_aside(opp):
    return get_set_aside_code(opp) in SET_ASIDE_CODES


def matches_psc(opp):
    """Verify returned PSC client-side even after server-side ccode filtering."""
    code = clean_text(opp.get("classificationCode")).upper()

    if not code:
        return False

    if any(code.startswith(bad_prefix) for bad_prefix in AVOID_PSC_CODES):
        return False

    return any(code.startswith(prefix) for prefix in PSC_CODES)


def passes_keyword_filter(opp):
    title = clean_text(opp.get("title")).upper()

    if any(keyword in title for keyword in EXCLUDE_TITLE_KEYWORDS):
        return False

    # Whole-word checks so we avoid false positives like RELEASE containing LEASE.
    if re.search(r"\bLEASE\b", title):
        return False

    for pattern in DLA_PART_TITLE_PATTERNS:
        if re.search(pattern, title):
            return False

    return True


def passes_deadline_filter(opp):
    """Keep unknown deadlines; drop opportunities due too soon or already past."""
    deadline_str = get_response_deadline(opp)
    if not deadline_str:
        return True

    try:
        deadline_dt = date_parser.parse(deadline_str)
    except (ValueError, TypeError, OverflowError):
        return True

    if deadline_dt.tzinfo is None:
        deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)

    hours_until = (deadline_dt - datetime.now(timezone.utc)).total_seconds() / 3600
    return hours_until > MIN_HOURS_UNTIL_DEADLINE


def build_sam_params(posted_from, posted_to, notice_type, psc_code, offset=0):
    return {
        "api_key": API_KEY,
        "postedFrom": posted_from,
        "postedTo": posted_to,
        "ptype": notice_type,
        "ccode": psc_code,
        "limit": RESULTS_PER_PAGE,
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# SAM.gov FETCH LOGIC
# ---------------------------------------------------------------------------


def fetch_page(params):
    """Fetch one page with retries."""
    resp = None
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            print(
                f"    Network error on attempt {attempt}/{MAX_RETRIES} "
                f"({e.__class__.__name__}). Retrying...",
                flush=True,
            )
            time.sleep(3 * attempt)

    if resp is None:
        raise last_error

    if resp.status_code == 429:
        raise RuntimeError("SAM.gov rate limit reached. Stopping for this run.")

    resp.raise_for_status()
    return resp.json()


def fetch_all_opportunities(posted_from, posted_to):
    """Fetch opportunities by PSC code and notice type.

    We use server-side PSC filtering through ccode, then still verify PSC and
    set-aside client-side. We do NOT query every set-aside code server-side,
    because that multiplies calls and can burn through API limits quickly.
    """
    all_records = []
    seen_notice_ids = set()
    api_calls = 0

    for psc_code in PSC_CODES:
        for notice_type in NOTICE_TYPES:
            offset = 0
            page_count = 0

            print(f"  Searching PSC {psc_code}, notice type {notice_type}...", flush=True)

            while True:
                if api_calls >= MAX_API_CALLS_PER_RUN:
                    print(
                        f"  Hit MAX_API_CALLS_PER_RUN={MAX_API_CALLS_PER_RUN}. "
                        "Stopping early for this run.",
                        flush=True,
                    )
                    return all_records

                params = build_sam_params(
                    posted_from=posted_from,
                    posted_to=posted_to,
                    notice_type=notice_type,
                    psc_code=psc_code,
                    offset=offset,
                )

                data = fetch_page(params)
                api_calls += 1

                records = data.get("opportunitiesData", []) or []
                total = int(data.get("totalRecords", 0) or 0)
                page_count += 1

                print(
                    f"    Page {page_count}: {len(records)} record(s) "
                    f"({min(offset + len(records), total)} of {total})",
                    flush=True,
                )

                for opp in records:
                    notice_id = clean_text(opp.get("noticeId"))

                    # Avoid duplicates because broad PSCs and exact PSCs can overlap.
                    if notice_id and notice_id in seen_notice_ids:
                        continue
                    if notice_id:
                        seen_notice_ids.add(notice_id)

                    opp["searched_psc_code"] = psc_code
                    opp["searched_notice_type"] = notice_type
                    all_records.append(opp)

                if not records:
                    break

                if len(records) < RESULTS_PER_PAGE:
                    break

                offset += RESULTS_PER_PAGE

                if page_count >= MAX_PAGES_PER_QUERY:
                    print(
                        f"    Hit page cap for PSC {psc_code}, notice type {notice_type}.",
                        flush=True,
                    )
                    break

                time.sleep(1)

    print(f"  API calls used this run: {api_calls}", flush=True)
    return all_records


def run_scan():
    posted_to = datetime.now(timezone.utc)
    posted_from = posted_to - timedelta(days=LOOKBACK_DAYS)
    posted_from_str = posted_from.strftime("%m/%d/%Y")
    posted_to_str = posted_to.strftime("%m/%d/%Y")

    seen_ids = load_seen_ids()
    new_matches = []

    print(f"Querying SAM.gov ({posted_from_str} - {posted_to_str})...", flush=True)

    try:
        records = fetch_all_opportunities(posted_from_str, posted_to_str)
    except (requests.exceptions.RequestException, RuntimeError) as e:
        print(f"Error fetching opportunities: {e}", flush=True)
        records = []

    print(f"Fetched {len(records)} total raw record(s) from SAM.gov.", flush=True)

    for opp in records:
        notice_id = clean_text(opp.get("noticeId"))
        if not notice_id or notice_id in seen_ids:
            continue
        if not matches_set_aside(opp):
            continue
        if not matches_psc(opp):
            continue
        if not passes_keyword_filter(opp):
            continue
        if not passes_deadline_filter(opp):
            continue

        new_matches.append(opp)
        seen_ids.add(notice_id)

    save_seen_ids(seen_ids)
    return new_matches


# ---------------------------------------------------------------------------
# OUTPUT GENERATION
# ---------------------------------------------------------------------------


def generate_csv(matches, filepath):
    fieldnames = [
        "title",
        "solicitationNumber",
        "noticeId",
        "classificationCode",
        "searched_psc_code",
        "type",
        "set_aside",
        "postedDate",
        "responseDeadLine",
        "agency",
        "naicsCode",
        "poc",
        "link",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for opp in matches:
            writer.writerow(
                {
                    "title": clean_text(opp.get("title")),
                    "solicitationNumber": clean_text(opp.get("solicitationNumber")),
                    "noticeId": clean_text(opp.get("noticeId")),
                    "classificationCode": clean_text(opp.get("classificationCode")),
                    "searched_psc_code": clean_text(opp.get("searched_psc_code")),
                    "type": clean_text(opp.get("type")),
                    "set_aside": get_set_aside_description(opp),
                    "postedDate": clean_text(opp.get("postedDate")),
                    "responseDeadLine": get_response_deadline(opp),
                    "agency": clean_text(opp.get("fullParentPathName") or opp.get("department")),
                    "naicsCode": clean_text(opp.get("naicsCode")),
                    "poc": get_poc_summary(opp),
                    "link": get_notice_link(opp),
                }
            )


def generate_plain_text_digest(matches):
    lines = [
        f"SAM.gov scan found {len(matches)} new low-hanging-fruit opportunity(ies):",
        "",
    ]

    for idx, opp in enumerate(matches, start=1):
        title = clean_text(opp.get("title")) or "Untitled"
        solnum = clean_text(opp.get("solicitationNumber")) or "Not listed"
        psc = clean_text(opp.get("classificationCode")) or "Not listed"
        set_aside = get_set_aside_description(opp) or "Not listed"
        deadline = get_response_deadline(opp) or "Not listed"
        link = get_notice_link(opp)

        lines.extend(
            [
                f"{idx}. {title}",
                f"   Solicitation: {solnum}",
                f"   PSC: {psc}",
                f"   Set-aside: {set_aside}",
                f"   Due: {deadline}",
                f"   Link: {link}",
                "",
            ]
        )

    return "\n".join(lines)


def generate_pdf(matches, filepath):
    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("SAM.gov New Opportunities", styles["Title"]))
    story.append(
        Paragraph(
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
            f"&mdash; {len(matches)} match(es)",
            styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            "Filters: small-business set-asides, selected equipment/supply PSCs, "
            f"deadline more than {MIN_HOURS_UNTIL_DEADLINE} hours out.",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 0.25 * inch))

    for opp in matches:
        title = escape(clean_text(opp.get("title")) or "Untitled")
        solnum = escape(clean_text(opp.get("solicitationNumber")) or "Not listed")
        agency = escape(clean_text(opp.get("fullParentPathName") or opp.get("department")) or "Unknown agency")
        notice_type = escape(clean_text(opp.get("type")) or "Unknown type")
        set_aside = escape(get_set_aside_description(opp) or "Not listed")
        psc = escape(clean_text(opp.get("classificationCode")) or "Not listed")
        naics = escape(clean_text(opp.get("naicsCode")) or "Not listed")
        posted = escape(clean_text(opp.get("postedDate")) or "Not listed")
        deadline = escape(get_response_deadline(opp) or "Not listed")
        poc = escape(get_poc_summary(opp) or "Not listed")
        link = escape(get_notice_link(opp))

        story.append(Paragraph(title, styles["Heading3"]))
        story.append(Paragraph(f"<b>Solicitation:</b> {solnum}", styles["Normal"]))
        story.append(Paragraph(f"<b>Agency:</b> {agency}", styles["Normal"]))
        story.append(
            Paragraph(
                f"<b>Type:</b> {notice_type} &nbsp;|&nbsp; "
                f"<b>Set-aside:</b> {set_aside}",
                styles["Normal"],
            )
        )
        story.append(
            Paragraph(
                f"<b>PSC:</b> {psc} &nbsp;|&nbsp; <b>NAICS:</b> {naics}",
                styles["Normal"],
            )
        )
        story.append(
            Paragraph(
                f"<b>Posted:</b> {posted} &nbsp;|&nbsp; <b>Response due:</b> {deadline}",
                styles["Normal"],
            )
        )
        story.append(Paragraph(f"<b>POC:</b> {poc}", styles["Normal"]))
        if link:
            story.append(Paragraph(f'<link href="{link}">{link}</link>', styles["Normal"]))
        story.append(Spacer(1, 0.1 * inch))
        story.append(HRFlowable(width="100%", color="#cccccc"))
        story.append(Spacer(1, 0.15 * inch))

    doc.build(story)


def attach_file(msg, filepath, filename=None):
    if not filepath or not os.path.exists(filepath):
        return

    filename = filename or os.path.basename(filepath)
    with open(filepath, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())

    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)


def send_email_with_outputs(matches):
    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        print("Email not configured (missing SMTP_USER/SMTP_PASS/EMAIL_TO). Skipping send.", flush=True)
        return

    generate_pdf(matches, PDF_PATH)
    generate_csv(matches, CSV_PATH)

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"SAM.gov new opportunities - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    body = generate_plain_text_digest(matches)
    msg.attach(MIMEText(body, "plain"))

    attach_file(msg, PDF_PATH, "sam_gov_opportunities.pdf")
    attach_file(msg, CSV_PATH, "sam_gov_opportunities.csv")

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    print(f"Email with PDF/CSV sent to {EMAIL_TO}.", flush=True)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main():
    if API_KEY == "PUT_YOUR_KEY_HERE":
        raise SystemExit("Set SAM_API_KEY environment variable before running.")

    matches = run_scan()

    if matches:
        send_email_with_outputs(matches)
    else:
        print("No new matching opportunities this run.", flush=True)


if __name__ == "__main__":
    main()
