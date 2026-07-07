#!/usr/bin/env python3
"""
sam_gov_bot_supply_equipment.py

Scans SAM.gov's public Opportunities API for low-hanging-fruit supply/equipment
opportunities only:

- supply/procurement centered
- small-business set-aside
- open/presolicitation/combined synopsis/solicitation notices
- excludes installation, construction, repair, service, maintenance, training,
  disposal, environmental, and professional-services work
- excludes most DLA/NSN commodity-part noise unless the title clearly reads like
  an actual equipment buy

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

Optional tuning:

   # 1 = scan more equipment PSCs; use this if your SAM.gov account has a higher request limit.
   export EXPANDED_EQUIPMENT_PSC="1"

   # Minimum time left before response deadline. Default: 24 hours.
   export MIN_HOURS_UNTIL_DEADLINE="24"

3. Run it once manually to test:
   python3 sam_gov_bot_supply_equipment.py

4. Schedule it with cron, for example daily at 7am:
   crontab -e
   0 7 * * * /usr/bin/python3 /path/to/sam_gov_bot_supply_equipment.py >> /path/to/sam_gov_bot.log 2>&1

RATE LIMITS
-----------
Public/basic API access may be limited. This script defaults to 10 PSC prefixes
so it stays compatible with lower request limits. If your account is verified
and has a higher limit, set EXPANDED_EQUIPMENT_PSC=1.
"""

import os
import json
import re
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

# How many days back to look each run. Keep this >= your run frequency so you
# never miss a posting even if a run fails.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))

# Optional deadline guard. This prevents the bot from emailing opportunities
# that are already due, due today, or practically impossible to quote.
MIN_HOURS_UNTIL_DEADLINE = int(os.environ.get("MIN_HOURS_UNTIL_DEADLINE", "24"))

# Notice types to include.
#   p = Presolicitation, k = Combined Synopsis/Solicitation, o = Solicitation
# We skip awards and sources-sought by default because they are not clean bid targets.
NOTICE_TYPES = ["p", "k", "o"]

# Lean PSC list: 10 equipment/supply-heavy buckets.
# This is designed to stay friendly to lower SAM.gov API limits.
LEAN_EQUIPMENT_PSC_CODES = [
    "34",  # Metalworking / machinery / shop machines
    "41",  # Refrigeration, air conditioning, air circulating equipment
    "49",  # Maintenance and repair shop equipment
    "63",  # Alarm, signal, security detection systems
    "65",  # Medical, dental, veterinary equipment and supplies
    "66",  # Instruments and laboratory equipment
    "67",  # Photographic, video, and camera equipment
    "71",  # Furniture
    "72",  # Household and commercial furnishings / appliances
    "73",  # Food preparation and serving equipment
]

# Expanded PSC list: broader equipment/supply net. Use only if your API quota
# supports the extra requests.
EXPANDED_EQUIPMENT_PSC_CODES = sorted(set(LEAN_EQUIPMENT_PSC_CODES + [
    "36",  # Special industry machinery
    "39",  # Materials handling equipment
    "42",  # Firefighting / rescue / safety equipment
    "44",  # Furnace / drying equipment
    "58",  # Communications / detection equipment
    "61",  # Electric wire / power distribution equipment
    "70",  # ADP / IT equipment / software / supplies
    "74",  # Office machines / text processing systems
    "75",  # Office supplies and devices
    "78",  # Recreational and athletic equipment
    "79",  # Cleaning equipment and supplies
]))

USE_EXPANDED_EQUIPMENT_PSC = os.environ.get("EXPANDED_EQUIPMENT_PSC", "0") == "1"
PSC_CODES = EXPANDED_EQUIPMENT_PSC_CODES if USE_EXPANDED_EQUIPMENT_PSC else LEAN_EQUIPMENT_PSC_CODES

# Small-business set-aside codes.
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

# Positive indicators: these titles usually describe goods/equipment buys,
# not labor-heavy work. Keep this practical and conservative.
SUPPLY_EQUIPMENT_INCLUDE_KEYWORDS = [
    # General equipment / systems
    "equipment", "system", "systems", "unit", "units", "kit", "kits",
    "machine", "machining center", "cnc", "workstation", "station",

    # Furniture / furnishings / office / storage
    "furniture", "chair", "chairs", "desk", "desks", "table", "tables",
    "cabinet", "cabinets", "shelving", "shelves", "shelf", "locker", "lockers",
    "office", "wardrobe", "bed tower", "sliding door", "door replacement",

    # Kitchen / appliances
    "kitchen", "freezer", "refrigerator", "refrigeration", "ice machine",
    "pot washer", "dishwasher", "oven", "furnace", "washer",

    # Lab / medical / scientific / industrial
    "laboratory", "lab", "microscope", "borescope", "cryostat", "laser",
    "exam light", "exam lights", "oximeter", "analyzer", "tape station",
    "vacuum oven", "flowmeter", "meter", "sensor", "tester", "test stand",
    "welding machine", "paint removal system", "de burring", "deburring",
    "polishing machine", "air heat-exchanger", "heat-exchanger", "heat exchanger",

    # Security / electronics / video
    "metal detector", "metal detectors", "access control", "security system",
    "camera", "camera module", "video", "motion capture", "tv", "tvs",
    "television", "monitor", "monitors", "spotlight", "spotlights",
    "radio analysis tool", "switch", "switches", "infiniBand", "power supplies",

    # Portable / delivery-type equipment
    "trailer", "generator", "fender", "fenders", "lights",
]

# Hard exclusions: these almost always mean labor, site work, continuing support,
# hazardous/licensed work, construction, or service-heavy performance.
LOW_HANGING_EXCLUDE_KEYWORDS = [
    "service", "services", "support services", "professional support",
    "maintenance", "preventative maintenance", "pm service", "repair", "repairs",
    "replace", "replacement", "renovate", "renovation", "construction",
    "install", "installation", "maintain", "upgrades", "upgrade",
    "fire suppression", "asphalt", "chimney", "roof", "roofing", "barracks",
    "building exterior", "parking garage", "road maintenance", "roadway",
    "trail repairs", "embankment", "retaining wall", "dam", "decommissioning",
    "wastewater", "water treatment", "sample analysis", "analytical",
    "regulatory compliance", "hazmat", "hazardous", "disposal", "pickup",
    "pumping", "septic", "vault pumping", "backflow", "valve replacement",
    "grounds", "ground maintenance", "weed", "pest", "rodent", "geese",
    "mastication", "seeding", "fuels reduction", "wild horse", "burro",
    "courier", "freight", "food services", "lodging", "workshop", "training",
    "instructor", "instructors", "advisory", "review", "volunteer",
    "reverse engineering", "fabrication", "machining/fabrication", "overhaul",
    "overhaul/upgrade", "in repair/modification of", "drawing", "drawings",
    "idiq", "bpa national", "national bpa", "lease", "rental",
]

# Commodity-part noise: these are usually exact NSN/DLA parts, not clean
# vendor-sourced equipment buys for Moonlit's current strategy.
COMMODITY_PART_KEYWORDS = [
    "locknut", "gasket", "seal", "ring,retaining", "retaining ring",
    "connector", "connector body", "connector,plug", "adapter", "quick disco",
    "bracket", "bearing", "pin", "quick release", "clamp", "sprocket",
    "valve,solenoid", "valve assembly", "coupling", "shaft", "cylinder assembly",
    "oil pump assembly", "engine block assembly", "frame,aircraft", "air cleaner",
    "anode assembly", "deck box operator", "cover, access", "tread,metallic",
    "cable assembly", "cable assy", "wiring harness", "waveguide assembly",
    "headset,electrical", "horn,loudspeaker", "backshell", "strainer element",
    "battery power suppl", "battery,rechargeable", "power supply, in repair",
    "panel,sonar", "panel,power distrib", "interface unit,comm",
    "circuit card", "hex rect assy", "pneumatic muffler", "pump,reciprocating",
]

RESULTS_PER_PAGE = 100
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


MAX_PAGES_PER_PSC = 5
MAX_RETRIES = 3
REQUEST_TIMEOUT = 45


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def get_opp_title(opp):
    return normalize_text(opp.get("title", ""))


def keyword_hit(text, keywords):
    return any(keyword.lower() in text for keyword in keywords)


def parse_deadline(deadline):
    """Return a datetime when possible, otherwise None."""
    if not deadline:
        return None

    raw = str(deadline).strip()
    if not raw:
        return None

    # SAM.gov commonly returns ISO strings like 2026-07-10T16:00:00-05:00
    # and sometimes date-only strings like 2026-07-17.
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass

    return None


def is_deadline_actionable(opp):
    deadline = parse_deadline(opp.get("responseDeadLine", ""))
    if deadline is None:
        # Keep undated opportunities; manually review them rather than missing them.
        return True

    if deadline.tzinfo is not None:
        now = datetime.now(deadline.tzinfo)
    else:
        now = datetime.utcnow()

    return deadline >= now + timedelta(hours=MIN_HOURS_UNTIL_DEADLINE)


def fetch_opportunities_for_psc(psc_code, posted_from, posted_to):
    """Fetch pages of opportunities for a single PSC code, capped at MAX_PAGES_PER_PSC.
    Retries transient network errors before giving up on that page."""
    all_records = []
    offset = 0
    page_count = 0

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

        resp = None
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_error = e
                print(
                    f"  PSC {psc_code}: network error on attempt {attempt}/{MAX_RETRIES} "
                    f"({e.__class__.__name__}). Retrying...",
                    flush=True,
                )
                time.sleep(3 * attempt)

        if resp is None:
            print(f"  PSC {psc_code}: giving up after {MAX_RETRIES} attempts, skipping.", flush=True)
            raise last_error

        if resp.status_code == 429:
            print(f"Rate limited on PSC {psc_code}. Stopping for this run.", flush=True)
            break
        resp.raise_for_status()

        data = resp.json()
        records = data.get("opportunitiesData", [])
        all_records.extend(records)
        page_count += 1

        total = data.get("totalRecords", 0)
        offset += RESULTS_PER_PAGE

        if page_count >= MAX_PAGES_PER_PSC:
            print(
                f"  PSC {psc_code}: hit page cap ({MAX_PAGES_PER_PSC} pages / "
                f"{page_count * RESULTS_PER_PAGE} records) out of {total} total. "
                f"Some results skipped this run -- consider narrowing PSC_CODES "
                f"or reducing LOOKBACK_DAYS if this happens often.",
                flush=True,
            )
            break

        if offset >= total or not records:
            break
        time.sleep(1)

    return all_records


def matches_set_aside(opp):
    return opp.get("typeOfSetAside") in SET_ASIDE_CODES


def matches_supply_equipment_low_hanging(opp):
    """Return (True, reason) only for clean supply/equipment procurement targets."""
    title = get_opp_title(opp)
    agency = normalize_text(opp.get("fullParentPathName") or opp.get("department", ""))
    text = f"{title} {agency}"

    if not title:
        return False, "missing title"

    if not matches_set_aside(opp):
        return False, "not a target small-business set-aside"

    if not is_deadline_actionable(opp):
        return False, "deadline too soon or already passed"

    if keyword_hit(text, LOW_HANGING_EXCLUDE_KEYWORDS):
        return False, "service/install/maintenance/construction exclusion"

    if keyword_hit(title, COMMODITY_PART_KEYWORDS):
        # Allow a few named equipment buys that include a generic word like "unit" or "lights",
        # but reject the typical DLA one-part NSN noise.
        if not keyword_hit(title, [
            "exam light", "exam lights", "metal detector", "metal detectors",
            "welding machine", "machine", "equipment", "system", "systems",
            "furniture", "shelving", "camera", "generator", "freezer",
            "ice machine", "kitchen", "tvs", "tv", "spotlight", "spotlights",
        ]):
            return False, "commodity/NSN part exclusion"

    # Titles like "28--LOCKNUT" or "59--ANTENNA" are usually one-off parts.
    # Keep them only when the title also clearly reads as an equipment/system buy.
    looks_like_numbered_part_title = bool(re.match(r"^\d{2,4}--", title))
    if looks_like_numbered_part_title and not keyword_hit(title, SUPPLY_EQUIPMENT_INCLUDE_KEYWORDS):
        return False, "numbered commodity title without equipment indicator"

    if keyword_hit(title, SUPPLY_EQUIPMENT_INCLUDE_KEYWORDS):
        return True, "supply/equipment title match"

    return False, "no clear supply/equipment indicator"


def run_scan():
    posted_to = datetime.utcnow()
    posted_from = posted_to - timedelta(days=LOOKBACK_DAYS)
    posted_from_str = posted_from.strftime("%m/%d/%Y")
    posted_to_str = posted_to.strftime("%m/%d/%Y")

    seen_ids = load_seen_ids()
    new_matches = []

    print(
        f"Scanning {len(PSC_CODES)} PSC equipment bucket(s): {', '.join(PSC_CODES)}",
        flush=True,
    )

    for psc in PSC_CODES:
        print(f"Querying PSC {psc} ({posted_from_str} - {posted_to_str})...", flush=True)
        try:
            records = fetch_opportunities_for_psc(psc, posted_from_str, posted_to_str)
        except requests.HTTPError as e:
            print(f"  Error fetching PSC {psc}: {e}", flush=True)
            continue

        print(f"  -> {len(records)} record(s) fetched for PSC {psc}", flush=True)

        for opp in records:
            notice_id = opp.get("noticeId")
            if not notice_id or notice_id in seen_ids:
                continue

            is_match, reason = matches_supply_equipment_low_hanging(opp)
            if not is_match:
                continue

            opp["_matchReason"] = reason
            opp["_pscQueried"] = psc
            new_matches.append(opp)
            seen_ids.add(notice_id)

        time.sleep(1)

    save_seen_ids(seen_ids)
    return new_matches


def format_email_body(matches):
    if not matches:
        return None

    lines = [
        f"SAM.gov scan found {len(matches)} new supply/equipment low-hanging-fruit opportunity(ies):\n"
    ]

    for opp in matches:
        title = opp.get("title", "Untitled")
        agency = opp.get("fullParentPathName") or opp.get("department", "Unknown agency")
        notice_type = opp.get("type", "Unknown type")
        set_aside = opp.get("typeOfSetAsideDescription") or opp.get("typeOfSetAside", "")
        posted = opp.get("postedDate", "")
        deadline = opp.get("responseDeadLine", "")
        notice_id = opp.get("noticeId", "")
        psc = opp.get("classificationCode") or opp.get("_pscQueried", "")
        reason = opp.get("_matchReason", "supply/equipment match")
        link = f"https://sam.gov/opp/{notice_id}/view" if notice_id else ""

        lines.append(
            f"- {title}\n"
            f"    Category: Supply / Equipment low-hanging fruit\n"
            f"    Match reason: {reason}\n"
            f"    PSC: {psc}\n"
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
    msg["Subject"] = f"SAM.gov supply/equipment opportunities - {datetime.utcnow().strftime('%Y-%m-%d')}"
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
        print("No new matching supply/equipment opportunities this run.")


if __name__ == "__main__":
    main()
