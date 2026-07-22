#!/usr/bin/env python3
"""
sam_gov_bot_delivery_supply_procurement.py

Scans SAM.gov's public Opportunities API for "low hanging fruit" contracts
that fit Nikolas's preferred lane:

- Delivery, supply, and procurement
- Little/no installation, assembly, reconfiguration, repair, maintenance, or training
- Small-business set-asides
- Active RFQ/open notice stages
- Enough time left to respond

This version intentionally filters out DLA/NSN/component/parts noise such as
cable assemblies, valves, switches, electronic components, parts kits, spares,
and anything that looks like install/replacement/reconfiguration work.

Output organization:
1. Newest posted / uploaded opportunities first
2. Pursue score second, highest first

It emails:
- A PDF digest of strong/conditional opportunities
- A CSV audit of every fetched notice and why it passed/rejected

SETUP
-----
Required environment variables:

    SAM_API_KEY="your_sam_gov_api_key"
    SMTP_USER="you@gmail.com"
    SMTP_PASS="your_gmail_app_password"
    EMAIL_TO="you@gmail.com"

Optional environment variables:

    SMTP_SERVER="smtp.gmail.com"
    SMTP_PORT="587"
    LOOKBACK_DAYS="2"
    MIN_HOURS_UNTIL_DEADLINE="36"
    MAX_DIGEST_ITEMS="50"
    INCLUDE_CONDITIONAL_IN_PDF="1"
    ALWAYS_EMAIL_SCAN_RESULTS="1"
    DISABLE_SEEN_FILTER="0"
    EXPANDED_EQUIPMENT_PSC="0"

DEPENDENCIES
------------
    pip install requests python-dateutil reportlab
"""

from __future__ import annotations

import csv
import json
import os
import re
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from xml.sax.saxutils import escape

import requests
from dateutil import parser as date_parser
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("SAM_API_KEY", "PUT_YOUR_KEY_HERE")
BASE_URL = os.environ.get("SAM_BASE_URL", "https://api.sam.gov/opportunities/v2/search")

# Dates / timing.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))
MIN_HOURS_UNTIL_DEADLINE = int(os.environ.get("MIN_HOURS_UNTIL_DEADLINE", "36"))

# Email behavior.
ALWAYS_EMAIL_SCAN_RESULTS = os.environ.get("ALWAYS_EMAIL_SCAN_RESULTS", "1") == "1"
INCLUDE_CONDITIONAL_IN_PDF = os.environ.get("INCLUDE_CONDITIONAL_IN_PDF", "1") == "1"
DISABLE_SEEN_FILTER = os.environ.get("DISABLE_SEEN_FILTER", "0") == "1"
MAX_DIGEST_ITEMS = int(os.environ.get("MAX_DIGEST_ITEMS", "50"))

# Notice types:
# p = Presolicitation
# k = Combined Synopsis/Solicitation
# o = Solicitation
# r = Sources Sought -- intentionally omitted unless you add it manually.
NOTICE_TYPES = ["p", "k", "o"]

# Product Service Codes / PSCs that are most likely to produce supply/equipment buys.
# SAM.gov request parameter is "ccode"; response field is "classificationCode".
PSC_CODES = [
    "23",    # Motor vehicles, trailers, cycles: only if purchase/delivery language passes
    "34",    # Metalworking machinery
    "35",    # Service and trade equipment
    "36",    # Special industry machinery - broad bucket
    "3695",  # Miscellaneous special industry machinery
    "38",    # Construction/mining/excavating/road maintenance equipment; equipment only
    "39",    # Materials handling equipment
    "41",    # Refrigeration / AC equipment; equipment only, no HVAC install
    "42",    # Firefighting / rescue / safety equipment
    "49",    # Maintenance and repair shop equipment
    "52",    # Measuring tools
    "56",    # Construction and building materials; supply-only only
    "63",    # Security / detection systems; no installation/replacement
    "65",    # Medical / dental / veterinary equipment
    "66",    # Instruments / lab equipment
    "67",    # Photographic / video equipment
    "71",    # Furniture
    "72",    # Household / commercial furnishings
    "73",    # Food prep / serving equipment
    "74",    # Office machines / business equipment
    "84",    # Clothing / individual equipment
    "95",    # Metal bars, sheets, shapes, etc.; supply-only only
]

# Optional wider equipment net. Leave off unless you want more noise.
EXPANDED_PSC_CODES = [
    "24",  # Tractors
    "37",  # Agricultural machinery/equipment
    "51",  # Hand tools
    "54",  # Prefabricated structures/scaffolding; often install/permitting risk
    "70",  # IT/software/equipment; often licensing/support risk
]

if os.environ.get("EXPANDED_EQUIPMENT_PSC", "0") == "1":
    PSC_CODES.extend(EXPANDED_PSC_CODES)

# PSCs that generally create parts/component/service noise rather than easy procurement.
AVOID_PSC_CODES = [
    "J",     # Maintenance / repair / rebuild of equipment
    "R",     # Professional / administrative services
    "S",     # Utilities / housekeeping / grounds services
    "Y",     # Construction
    "Z",     # Maintenance / repair of real property
    "25",    # Vehicular equipment components
    "28",    # Engines / turbines / components
    "29",    # Engine accessories
    "30",    # Mechanical power transmission equipment
    "31",    # Bearings
    "47",    # Pipe, tubing, hose, fittings
    "48",    # Valves
    "53",    # Hardware / fasteners
    "59",    # Electrical/electronic equipment components
    "596",   # Electronic components
    "597",   # Electrical hardware/supplies
    "599",   # Misc electrical/electronic components
    "61",    # Electric wire / power distribution equipment
    "6150",  # Cable / cord / wire assemblies
    "62",    # Lighting fixtures/lamps; often parts
    "68",    # Chemicals/gases; hazmat/recurring-delivery risk
]

# Small-business set-aside codes to keep.
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

# Hard title exclusions: these usually mean the work is not simple delivery/supply.
HARD_EXCLUDE_TITLE_PATTERNS = [
    r"\bASSEMBL(Y|IES)\b",
    r"\bASSY\b",
    r"\bCABLE\b",
    r"\bCOUPLING\b",
    r"\bCOMPONENTS?\b",
    r"\bELECTRONIC COMPONENTS?\b",
    r"\bPARTS\b",
    r"\bPARTS KIT\b",
    r"\bSPARES?\b",
    r"\bVALVE\b",
    r"\bSWITCH\b",
    r"\bSOLENOID\b",
    r"\bBATTERY\b",
    r"\bRETAINER\b",
    r"\bNSN\b",
    r"\bNOUN\b",
    r"^\s*\d{2}\s*[-–—]",             # DLA-style title: 48--VALVE, 59--SWITCH
    r"\bRE[- ]?KEY\b",
    r"\bCORE REPLACEMENT\b",
    r"\bRECONFIGURATION\b",
    r"\bRECONFIGURE\b",
    r"\bREPLACEMENT\b",
    r"\bREPAIR\b",
    r"\bMAINTENANCE\b",
    r"\bMAINT\b",
    r"\bMODIFICATION\b",
    r"\bOVERHAUL\b",
    r"\bREFURB(ISH|ISHMENT)?\b",
    r"\bINSTALL\b",
    r"\bINSTALLATION\b",
    r"\bSTART[- ]?UP\b",
    r"\bCALIBRATION\b",
    r"\bREMOVAL\b",
    r"\bDISPOSAL\b",
    r"\bDEMO(LITION)?\b",
    r"\bRENOVATION\b",
    r"\bRENOVATE\b",
    r"\bCONSTRUCTION\b",
    r"\bBUILDING\b",
    r"\bBLDGS?\b",
    r"\bPREFAB\b",
    r"\bSTRUCTURE\b",
    r"\bSERVICE(S)?\b",
    r"\bSUPPORT SERVICES\b",
    r"\bTRAINING\b",
    r"\bSITE VISIT\b",
    r"\bINSPECTION\b",
    r"\bTESTING\b",
    r"\bCOMPLIANCE\b",
    r"\bADVISORY\b",
    r"\bABATEMENT\b",
    r"\bMITIGATION\b",
    r"\bCLEANING\b",
    r"\bPEST CONTROL\b",
    r"\bMOWING\b",
    r"\bVEGETATION\b",
    r"\bLODGING\b",
    r"\bRENTAL\b",
    r"\bLEASE\b",
    r"\bTANK RENT\b",
]

# Positive language that must be present for a title to be considered in-scope.
GOOD_SUPPLY_TITLE_PATTERNS = [
    r"\bSUPPLY\b",
    r"\bSUPPLIES\b",
    r"\bDELIVER\b",
    r"\bDELIVERY\b",
    r"\bPURCHASE\b",
    r"\bPROCUREMENT\b",
    r"\bEQUIPMENT\b",
    r"\bMACHINE\b",
    r"\bMACHINES\b",
    r"\bSYSTEM\b",
    r"\bSYSTEMS\b",
    r"\bFURNITURE\b",
    r"\bCABINET\b",
    r"\bTABLE\b",
    r"\bCART\b",
    r"\bSHELV(ING|ES)?\b",
    r"\bFREEZER\b",
    r"\bREFRIGERATOR\b",
    r"\bOVEN\b",
    r"\bWASHER\b",
    r"\bGENERATOR\b",
    r"\bSNOWMOBILE\b",
    r"\bATV\b",
    r"\bBOOTS\b",
    r"\bHELMETS?\b",
    r"\bFENCE MATERIALS\b",
    r"\bFABRICATED STEEL SUPPLY\b",
]

# Higher priority phrases within the supply lane.
BEST_SUPPLY_TITLE_PATTERNS = [
    r"\bPURCHASE AND DELIVERY\b",
    r"\bSUPPLY AND DELIVER\b",
    r"\bMATERIALS SUPPLY\b",
    r"\bEQUIPMENT PURCHASE\b",
    r"\bOFFICE FURNITURE\b",
    r"\bMEDICAL EQUIPMENT\b",
    r"\bOPHTHALMIC EQUIPMENT\b",
    r"\bLAB(ORATORY)? EQUIPMENT\b",
]

# Agency/solicitation patterns that almost always mean parts/NSN/DLA noise.
DLA_AGENCY_PATTERN = re.compile(r"DEFENSE LOGISTICS AGENCY|DLA\b", re.IGNORECASE)
DLA_SOLICITATION_PREFIXES = ("SPE", "SPR")

# API safety controls.
MAX_PAGES_PER_QUERY = int(os.environ.get("MAX_PAGES_PER_QUERY", "10"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "45"))
RESULTS_PER_PAGE = int(os.environ.get("RESULTS_PER_PAGE", "1000"))
MAX_API_CALLS_PER_RUN = int(os.environ.get("MAX_API_CALLS_PER_RUN", "250"))

# Local output files live beside the script so GitHub Actions/cron can run from any directory.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.environ.get("SEEN_FILE", os.path.join(SCRIPT_DIR, "seen_notices.json"))
PDF_PATH = os.environ.get("PDF_PATH", os.path.join(SCRIPT_DIR, "sam_gov_low_hanging_opportunities.pdf"))
CSV_PATH = os.environ.get("CSV_PATH", os.path.join(SCRIPT_DIR, "sam_gov_scan_audit.csv"))

# Email settings.
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER)


# ---------------------------------------------------------------------------
# DATA STRUCTURES
# ---------------------------------------------------------------------------


@dataclass
class Evaluation:
    status: str
    pursue_score: int
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejection_reason: str = ""


# ---------------------------------------------------------------------------
# BASIC HELPERS
# ---------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_upper(value: Any) -> str:
    return clean_text(value).upper()


def parse_date(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        dt = date_parser.parse(text)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def title_matches_any(title: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in patterns)


def matched_patterns(title: str, patterns: list[str]) -> list[str]:
    found = []
    for pattern in patterns:
        if re.search(pattern, title, flags=re.IGNORECASE):
            found.append(pattern)
    return found


def load_seen_state() -> dict[str, str]:
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    # Backward compatible with old seen_notices.json list format.
    if isinstance(data, list):
        return {clean_text(notice_id): "" for notice_id in data if clean_text(notice_id)}
    if isinstance(data, dict):
        return {clean_text(k): clean_text(v) for k, v in data.items() if clean_text(k)}
    return {}


def save_seen_state(seen_state: dict[str, str]) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(seen_state.items())), f, indent=2)


def get_set_aside_code(opp: dict[str, Any]) -> str:
    return normalize_upper(opp.get("typeOfSetAside") or opp.get("setAsideCode") or "")


def get_set_aside_description(opp: dict[str, Any]) -> str:
    return clean_text(
        opp.get("typeOfSetAsideDescription")
        or opp.get("setAside")
        or get_set_aside_code(opp)
    )


def get_response_deadline(opp: dict[str, Any]) -> str:
    # SAM has historically exposed this with inconsistent capitalization/spelling.
    return clean_text(
        opp.get("responseDeadLine")
        or opp.get("responseDeadline")
        or opp.get("reponseDeadLine")
    )


def get_posted_date(opp: dict[str, Any]) -> str:
    return clean_text(opp.get("postedDate") or opp.get("posteddate"))


def get_modified_date(opp: dict[str, Any]) -> str:
    return clean_text(opp.get("modifiedDate") or opp.get("modifieddate") or opp.get("archiveDate"))


def get_notice_link(opp: dict[str, Any]) -> str:
    notice_id = clean_text(opp.get("noticeId"))
    ui_link = clean_text(opp.get("uiLink"))

    if ui_link and ui_link.lower() not in {"null", "none"}:
        return ui_link
    if notice_id:
        return f"https://sam.gov/opp/{notice_id}/view"
    return ""


def get_agency(opp: dict[str, Any]) -> str:
    return clean_text(opp.get("fullParentPathName") or opp.get("department") or opp.get("organizationName"))


def get_poc_summary(opp: dict[str, Any]) -> str:
    contacts = opp.get("pointOfContact") or opp.get("pointofContact") or []
    if not isinstance(contacts, list):
        return ""

    pieces = []
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        name = clean_text(contact.get("fullName") or contact.get("fullname") or contact.get("name"))
        email = clean_text(contact.get("email"))
        phone = clean_text(contact.get("phone"))
        parts = [part for part in [name, email, phone] if part]
        if parts:
            pieces.append(" / ".join(parts))

    return "; ".join(pieces)


def get_notice_signature(opp: dict[str, Any]) -> str:
    fields = [
        clean_text(opp.get("noticeId")),
        clean_text(opp.get("solicitationNumber")),
        clean_text(opp.get("title")),
        get_posted_date(opp),
        get_modified_date(opp),
        get_response_deadline(opp),
        clean_text(opp.get("classificationCode")),
        get_set_aside_code(opp),
    ]
    return "|".join(fields)


# ---------------------------------------------------------------------------
# FILTERS AND SCORING
# ---------------------------------------------------------------------------


def is_target_psc(opp: dict[str, Any]) -> bool:
    code = normalize_upper(opp.get("classificationCode"))
    if not code:
        return False
    if any(code.startswith(bad_prefix) for bad_prefix in AVOID_PSC_CODES):
        return False
    return any(code.startswith(prefix) for prefix in PSC_CODES)


def is_small_business_set_aside(opp: dict[str, Any]) -> bool:
    return get_set_aside_code(opp) in SET_ASIDE_CODES


def is_dla_parts_noise(opp: dict[str, Any]) -> bool:
    agency = get_agency(opp)
    solicitation = normalize_upper(opp.get("solicitationNumber"))
    title = normalize_upper(opp.get("title"))

    if DLA_AGENCY_PATTERN.search(agency):
        return True
    if solicitation.startswith(DLA_SOLICITATION_PREFIXES):
        return True
    if re.search(r"^\s*\d{2}\s*[-–—]", title):
        return True
    if "NSN" in title:
        return True
    return False


def has_good_supply_language(opp: dict[str, Any]) -> bool:
    title = normalize_upper(opp.get("title"))
    return title_matches_any(title, GOOD_SUPPLY_TITLE_PATTERNS)


def has_hard_title_exclusion(opp: dict[str, Any]) -> tuple[bool, list[str]]:
    title = normalize_upper(opp.get("title"))
    patterns = matched_patterns(title, HARD_EXCLUDE_TITLE_PATTERNS)
    return bool(patterns), patterns


def deadline_hours(opp: dict[str, Any]) -> float | None:
    deadline_dt = parse_date(get_response_deadline(opp))
    if not deadline_dt:
        return None
    return (deadline_dt - datetime.now(timezone.utc)).total_seconds() / 3600


def evaluate_opportunity(opp: dict[str, Any]) -> Evaluation:
    title = normalize_upper(opp.get("title"))
    code = normalize_upper(opp.get("classificationCode"))
    solicitation = normalize_upper(opp.get("solicitationNumber"))

    reasons: list[str] = []
    warnings: list[str] = []
    score = 0

    if not solicitation or "Q" not in solicitation:
        warnings.append("Solicitation number does not clearly look like an RFQ.")
    else:
        score += 1
        reasons.append("RFQ-style solicitation number.")

    if not is_small_business_set_aside(opp):
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason=f"Not a target small-business set-aside: {get_set_aside_code(opp) or 'blank'}.",
        )
    score += 2
    reasons.append("Eligible small-business set-aside.")

    if not is_target_psc(opp):
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason=f"PSC {code or 'blank'} is not in the delivery/supply target list or is excluded as parts/service noise.",
        )
    score += 2
    reasons.append(f"Target supply/equipment PSC {code}.")

    if is_dla_parts_noise(opp):
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason="DLA/SPE/SPR/NSN-style parts opportunity; not clean delivery/supply procurement.",
        )

    hard_excluded, patterns = has_hard_title_exclusion(opp)
    if hard_excluded:
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason="Title indicates parts/assembly/install/replacement/service work: " + ", ".join(patterns[:5]),
        )

    if not has_good_supply_language(opp):
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason="Title lacks clear delivery/supply/procurement/equipment language.",
        )
    score += 3
    reasons.append("Clear delivery/supply/procurement/equipment language in title.")

    if title_matches_any(title, BEST_SUPPLY_TITLE_PATTERNS):
        score += 2
        reasons.append("High-fit supply phrase in title.")

    hours = deadline_hours(opp)
    if hours is None:
        score += 1
        warnings.append("No parseable response deadline; verify manually.")
    elif hours <= 0:
        return Evaluation(status="rejected", pursue_score=0, rejection_reason="Response deadline has passed.")
    elif hours <= MIN_HOURS_UNTIL_DEADLINE:
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason=f"Due too soon: {hours:.1f} hours until response deadline.",
        )
    elif hours >= 14 * 24:
        score += 2
        reasons.append("At least 14 days remain.")
    elif hours >= 7 * 24:
        score += 1
        reasons.append("At least 7 days remain.")
    else:
        warnings.append("Less than 7 days remain; only pursue if supplier path is immediate.")

    # Best-fit PSC boosts.
    if code.startswith(("35", "36", "3695", "39", "49", "65", "66", "71", "73", "84")):
        score += 1
        reasons.append("PSC is in a historically practical procurement lane.")

    # Extra caution even if not hard-rejected.
    if any(word in title for word in ["CUSTOM", "DESIGN", "FABRICATE", "FABRICATION"]):
        warnings.append("Potential custom/fabrication requirement; verify supplier can quote quickly.")
        score -= 1

    if any(word in title for word in ["HAZMAT", "HAZARDOUS", "PROPANE", "HELIUM"]):
        warnings.append("Potential hazmat/regulated delivery; verify logistics requirements.")
        score -= 2

    if score >= 9:
        status = "strong"
    elif score >= 7:
        status = "conditional"
    else:
        status = "rejected"

    rejection_reason = "" if status != "rejected" else "Score below pursuit threshold after delivery/supply fit check."
    return Evaluation(status=status, pursue_score=max(score, 0), reasons=reasons, warnings=warnings, rejection_reason=rejection_reason)


def sort_key_for_opportunity(opp: dict[str, Any]) -> tuple[float, int, str]:
    posted_dt = parse_date(get_posted_date(opp))
    posted_ts = posted_dt.timestamp() if posted_dt else 0.0
    eval_score = int(opp.get("pursue_score", 0) or 0)
    title = clean_text(opp.get("title"))
    # Python sorts ascending, so use negative timestamp and negative score.
    return (-posted_ts, -eval_score, title)


# ---------------------------------------------------------------------------
# SAM.gov FETCH LOGIC
# ---------------------------------------------------------------------------


def build_sam_params(posted_from: str, posted_to: str, notice_type: str, psc_code: str, offset: int = 0) -> dict[str, Any]:
    return {
        "api_key": API_KEY,
        "postedFrom": posted_from,
        "postedTo": posted_to,
        "ptype": notice_type,
        "ccode": psc_code,
        "limit": RESULTS_PER_PAGE,
        "offset": offset,
    }


def fetch_page(params: dict[str, Any]) -> dict[str, Any]:
    response = None
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            print(
                f"    Network error on attempt {attempt}/{MAX_RETRIES} "
                f"({exc.__class__.__name__}). Retrying...",
                flush=True,
            )
            time.sleep(3 * attempt)

    if response is None:
        raise last_error or RuntimeError("Unknown request error")

    if response.status_code == 429:
        raise RuntimeError("SAM.gov rate limit reached. Stopping for this run.")

    response.raise_for_status()
    return response.json()


def fetch_all_opportunities(posted_from: str, posted_to: str) -> list[dict[str, Any]]:
    all_records: list[dict[str, Any]] = []
    seen_notice_ids: set[str] = set()
    api_calls = 0

    for psc_code in PSC_CODES:
        for notice_type in NOTICE_TYPES:
            offset = 0
            page_count = 0
            print(f"  Searching PSC {psc_code}, notice type {notice_type}...", flush=True)

            while True:
                if api_calls >= MAX_API_CALLS_PER_RUN:
                    print(
                        f"  Hit MAX_API_CALLS_PER_RUN={MAX_API_CALLS_PER_RUN}. Stopping early.",
                        flush=True,
                    )
                    print(f"  API calls used this run: {api_calls}", flush=True)
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
                    if not isinstance(opp, dict):
                        continue
                    notice_id = clean_text(opp.get("noticeId"))
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


def run_scan() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    posted_to = datetime.now(timezone.utc)
    posted_from = posted_to - timedelta(days=LOOKBACK_DAYS)
    posted_from_str = posted_from.strftime("%m/%d/%Y")
    posted_to_str = posted_to.strftime("%m/%d/%Y")

    seen_state = load_seen_state()
    digest_matches: list[dict[str, Any]] = []
    all_passing_matches: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []

    print(f"Querying SAM.gov ({posted_from_str} - {posted_to_str})...", flush=True)

    try:
        records = fetch_all_opportunities(posted_from_str, posted_to_str)
    except (requests.exceptions.RequestException, RuntimeError) as exc:
        print(f"Error fetching opportunities: {exc}", flush=True)
        records = []

    print(f"Fetched {len(records)} total raw record(s) from SAM.gov.", flush=True)

    for opp in records:
        notice_id = clean_text(opp.get("noticeId"))
        evaluation = evaluate_opportunity(opp)

        opp["pursue_status"] = evaluation.status
        opp["pursue_score"] = evaluation.pursue_score
        opp["pursue_reasons"] = "; ".join(evaluation.reasons)
        opp["pursue_warnings"] = "; ".join(evaluation.warnings)
        opp["rejection_reason"] = evaluation.rejection_reason

        signature = get_notice_signature(opp)
        already_seen_same_version = bool(notice_id and seen_state.get(notice_id) == signature)

        audit_rows.append(opp)

        if evaluation.status in {"strong", "conditional"}:
            all_passing_matches.append(opp)

            if DISABLE_SEEN_FILTER or not already_seen_same_version:
                digest_matches.append(opp)

            if notice_id:
                seen_state[notice_id] = signature

    # Sort primarily by newest posted date, then by pursue score.
    digest_matches.sort(key=sort_key_for_opportunity)
    all_passing_matches.sort(key=sort_key_for_opportunity)
    audit_rows.sort(key=sort_key_for_opportunity)

    save_seen_state(seen_state)

    return digest_matches, all_passing_matches, audit_rows


# ---------------------------------------------------------------------------
# OUTPUT GENERATION
# ---------------------------------------------------------------------------


def summarize_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"strong": 0, "conditional": 0, "rejected": 0}
    for row in rows:
        status = clean_text(row.get("pursue_status")) or "rejected"
        counts[status] = counts.get(status, 0) + 1
    return counts


def generate_csv(audit_rows: list[dict[str, Any]], filepath: str) -> None:
    fieldnames = [
        "pursue_status",
        "pursue_score",
        "postedDate",
        "responseDeadLine",
        "title",
        "solicitationNumber",
        "noticeId",
        "classificationCode",
        "searched_psc_code",
        "searched_notice_type",
        "type",
        "set_aside_code",
        "set_aside_description",
        "agency",
        "naicsCode",
        "poc",
        "link",
        "pursue_reasons",
        "pursue_warnings",
        "rejection_reason",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for opp in audit_rows:
            writer.writerow(
                {
                    "pursue_status": clean_text(opp.get("pursue_status")),
                    "pursue_score": clean_text(opp.get("pursue_score")),
                    "postedDate": get_posted_date(opp),
                    "responseDeadLine": get_response_deadline(opp),
                    "title": clean_text(opp.get("title")),
                    "solicitationNumber": clean_text(opp.get("solicitationNumber")),
                    "noticeId": clean_text(opp.get("noticeId")),
                    "classificationCode": clean_text(opp.get("classificationCode")),
                    "searched_psc_code": clean_text(opp.get("searched_psc_code")),
                    "searched_notice_type": clean_text(opp.get("searched_notice_type")),
                    "type": clean_text(opp.get("type")),
                    "set_aside_code": get_set_aside_code(opp),
                    "set_aside_description": get_set_aside_description(opp),
                    "agency": get_agency(opp),
                    "naicsCode": clean_text(opp.get("naicsCode")),
                    "poc": get_poc_summary(opp),
                    "link": get_notice_link(opp),
                    "pursue_reasons": clean_text(opp.get("pursue_reasons")),
                    "pursue_warnings": clean_text(opp.get("pursue_warnings")),
                    "rejection_reason": clean_text(opp.get("rejection_reason")),
                }
            )


def get_pdf_items(digest_matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for opp in digest_matches:
        status = clean_text(opp.get("pursue_status"))
        if status == "strong":
            items.append(opp)
        elif status == "conditional" and INCLUDE_CONDITIONAL_IN_PDF:
            items.append(opp)
    return items[:MAX_DIGEST_ITEMS]


def generate_plain_text_digest(digest_matches: list[dict[str, Any]], audit_rows: list[dict[str, Any]]) -> str:
    counts = summarize_counts(audit_rows)
    pdf_items = get_pdf_items(digest_matches)

    if not digest_matches:
        return (
            "SAM.gov delivery/supply/procurement scan completed successfully.\n\n"
            "No new or materially updated matching opportunities were found this run.\n\n"
            f"All fetched RFQ-stage notices evaluated: Strong {counts.get('strong', 0)}, "
            f"Conditional {counts.get('conditional', 0)}, Rejected {counts.get('rejected', 0)}.\n\n"
            "The attached CSV audit lists every fetched notice and the exact filter reason.\n"
        )

    lines = [
        "SAM.gov delivery/supply/procurement scan complete.",
        "",
        "New/materially updated items attached, sorted by newest posted date first, then pursue score:",
        f"- Digest items: {len(pdf_items)}",
        f"- Strong RFQs in fetched pool: {counts.get('strong', 0)}",
        f"- Conditional RFQs in fetched pool: {counts.get('conditional', 0)}",
        f"- Rejected notices in fetched pool: {counts.get('rejected', 0)}",
        "",
    ]

    for idx, opp in enumerate(pdf_items, start=1):
        warnings = clean_text(opp.get("pursue_warnings"))
        lines.extend(
            [
                f"{idx}. [{clean_text(opp.get('pursue_status')).upper()} / Score {clean_text(opp.get('pursue_score'))}] {clean_text(opp.get('title'))}",
                f"   Solicitation: {clean_text(opp.get('solicitationNumber')) or 'Not listed'}",
                f"   Posted: {get_posted_date(opp) or 'Not listed'} | Due: {get_response_deadline(opp) or 'Not listed'}",
                f"   PSC: {clean_text(opp.get('classificationCode')) or 'Not listed'} | Set-aside: {get_set_aside_description(opp) or 'Not listed'}",
                f"   Link: {get_notice_link(opp)}",
            ]
        )
        if warnings:
            lines.append(f"   Watch: {warnings}")
        lines.append("")

    lines.append("The attached CSV audit lists every fetched notice and the exact filter reason.")
    return "\n".join(lines)


def add_opportunity_to_story(story: list[Any], styles: dict[str, Any], opp: dict[str, Any]) -> None:
    title = escape(clean_text(opp.get("title")) or "Untitled")
    solnum = escape(clean_text(opp.get("solicitationNumber")) or "Not listed")
    agency = escape(get_agency(opp) or "Unknown agency")
    notice_type = escape(clean_text(opp.get("type")) or "Unknown type")
    set_aside = escape(get_set_aside_description(opp) or "Not listed")
    psc = escape(clean_text(opp.get("classificationCode")) or "Not listed")
    naics = escape(clean_text(opp.get("naicsCode")) or "Not listed")
    posted = escape(get_posted_date(opp) or "Not listed")
    deadline = escape(get_response_deadline(opp) or "Not listed")
    poc = escape(get_poc_summary(opp) or "Not listed")
    score = escape(clean_text(opp.get("pursue_score")) or "0")
    status = escape(clean_text(opp.get("pursue_status")).upper() or "UNKNOWN")
    reasons = escape(clean_text(opp.get("pursue_reasons")) or "Not listed")
    warnings = escape(clean_text(opp.get("pursue_warnings")) or "None")
    link = escape(get_notice_link(opp))

    story.append(Paragraph(f"[{status} | Score {score}] {title}", styles["Heading3"]))
    story.append(Paragraph(f"<b>Solicitation:</b> {solnum}", styles["Normal"]))
    story.append(Paragraph(f"<b>Posted:</b> {posted} &nbsp;|&nbsp; <b>Response due:</b> {deadline}", styles["Normal"]))
    story.append(Paragraph(f"<b>Agency:</b> {agency}", styles["Normal"]))
    story.append(Paragraph(f"<b>Type:</b> {notice_type} &nbsp;|&nbsp; <b>Set-aside:</b> {set_aside}", styles["Normal"]))
    story.append(Paragraph(f"<b>PSC:</b> {psc} &nbsp;|&nbsp; <b>NAICS:</b> {naics}", styles["Normal"]))
    story.append(Paragraph(f"<b>POC:</b> {poc}", styles["Normal"]))
    story.append(Paragraph(f"<b>Why it surfaced:</b> {reasons}", styles["Normal"]))
    story.append(Paragraph(f"<b>Watch:</b> {warnings}", styles["Normal"]))
    if link:
        story.append(Paragraph(f'<link href="{link}">{link}</link>', styles["Normal"]))
    story.append(Spacer(1, 0.1 * inch))
    story.append(HRFlowable(width="100%", color="#cccccc"))
    story.append(Spacer(1, 0.15 * inch))


def generate_pdf(digest_matches: list[dict[str, Any]], audit_rows: list[dict[str, Any]], filepath: str) -> None:
    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
    )
    styles = getSampleStyleSheet()
    story: list[Any] = []
    counts = summarize_counts(audit_rows)
    pdf_items = get_pdf_items(digest_matches)

    story.append(Paragraph("SAM.gov Delivery / Supply / Procurement RFQs", styles["Title"]))
    story.append(
        Paragraph(
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
            f"| New/material updates in digest: {len(pdf_items)}",
            styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            "Sorted by newest posted date first, then pursue score. Filters remove assembly, parts, "
            "components, DLA/NSN-style buys, install, replacement, reconfiguration, repair, maintenance, "
            "training, and service-heavy work.",
            styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            f"All fetched notices evaluated: Strong {counts.get('strong', 0)} | "
            f"Conditional {counts.get('conditional', 0)} | Rejected {counts.get('rejected', 0)}",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 0.25 * inch))

    if not pdf_items:
        story.append(Paragraph("No new or materially updated matching opportunities found this run.", styles["Heading2"]))
        story.append(Paragraph("See the attached CSV audit for raw fetched notices and filter reasons.", styles["Normal"]))
    else:
        strong_items = [opp for opp in pdf_items if clean_text(opp.get("pursue_status")) == "strong"]
        conditional_items = [opp for opp in pdf_items if clean_text(opp.get("pursue_status")) == "conditional"]

        if strong_items:
            story.append(Paragraph("Strong RFQs", styles["Heading2"]))
            for opp in strong_items:
                add_opportunity_to_story(story, styles, opp)

        if conditional_items:
            story.append(PageBreak())
            story.append(Paragraph("Conditional RFQs", styles["Heading2"]))
            for opp in conditional_items:
                add_opportunity_to_story(story, styles, opp)

    doc.build(story)


def attach_file(msg: MIMEMultipart, filepath: str, filename: str | None = None) -> None:
    if not filepath or not os.path.exists(filepath):
        return

    filename = filename or os.path.basename(filepath)
    with open(filepath, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())

    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)


def send_email_with_outputs(digest_matches: list[dict[str, Any]], audit_rows: list[dict[str, Any]]) -> None:
    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        print("Email not configured (missing SMTP_USER/SMTP_PASS/EMAIL_TO). Skipping send.", flush=True)
        return

    generate_pdf(digest_matches, audit_rows, PDF_PATH)
    generate_csv(audit_rows, CSV_PATH)

    counts = summarize_counts(audit_rows)
    subject_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = (
        f"SAM.gov delivery/supply opportunities — "
        f"{counts.get('strong', 0)} strong, {counts.get('conditional', 0)} conditional — {subject_date}"
    )

    body = generate_plain_text_digest(digest_matches, audit_rows)
    msg.attach(MIMEText(body, "plain"))

    attach_file(msg, PDF_PATH, "sam_gov_low_hanging_opportunities.pdf")
    attach_file(msg, CSV_PATH, "sam_gov_scan_audit.csv")

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    print(f"Email with scan results sent to {EMAIL_TO}.", flush=True)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main() -> None:
    if API_KEY == "PUT_YOUR_KEY_HERE":
        raise SystemExit("Set SAM_API_KEY environment variable before running.")

    digest_matches, all_passing_matches, audit_rows = run_scan()

    print(
        "Scan results: "
        f"{len([x for x in audit_rows if x.get('pursue_status') == 'strong'])} strong, "
        f"{len([x for x in audit_rows if x.get('pursue_status') == 'conditional'])} conditional, "
        f"{len([x for x in audit_rows if x.get('pursue_status') == 'rejected'])} rejected. "
        f"{len(digest_matches)} new/materially updated digest item(s).",
        flush=True,
    )

    if ALWAYS_EMAIL_SCAN_RESULTS or digest_matches:
        send_email_with_outputs(digest_matches, audit_rows)
    else:
        print("No new matching opportunities this run.", flush=True)


if __name__ == "__main__":
    main()
