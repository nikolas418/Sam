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

FIXES INCLUDED:
1. Flexible Small-Business Set-Aside parser (handles full descriptions & codes).
2. Refined exclusion rules (prevents false positives on COTS replacement buys).
3. PSC-first validation (doesn't reject standard product titles lacking exact buzzwords).
4. Balanced scoring system (realistic pass thresholds).
5. Comprehensive diagnostic logging.

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
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("SAM_API_KEY", "PUT_YOUR_KEY_HERE")
BASE_URL = os.environ.get("SAM_BASE_URL", "https://api.sam.gov/opportunities/v2/search")

# Dates / timing.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))
MIN_HOURS_UNTIL_DEADLINE = int(os.environ.get("MIN_HOURS_UNTIL_DEADLINE", "12"))

# Email behavior.
ALWAYS_EMAIL_SCAN_RESULTS = os.environ.get("ALWAYS_EMAIL_SCAN_RESULTS", "1") == "1"
INCLUDE_CONDITIONAL_IN_PDF = os.environ.get("INCLUDE_CONDITIONAL_IN_PDF", "1") == "1"
DISABLE_SEEN_FILTER = os.environ.get("DISABLE_SEEN_FILTER", "0") == "1"
MAX_DIGEST_ITEMS = int(os.environ.get("MAX_DIGEST_ITEMS", "50"))

# Notice types:
# p = Presolicitation, k = Combined Synopsis/Solicitation, o = Solicitation
NOTICE_TYPES = ["p", "k", "o"]

# Product Service Codes / PSCs for supply/equipment buys.
PSC_CODES = [
    "23",    # Motor vehicles, trailers, cycles
    "34",    # Metalworking machinery
    "35",    # Service and trade equipment
    "36",    # Special industry machinery
    "3695",  # Miscellaneous special industry machinery
    "38",    # Construction/mining/excavating equipment
    "39",    # Materials handling equipment
    "41",    # Refrigeration / AC equipment
    "42",    # Firefighting / rescue / safety equipment
    "49",    # Maintenance and repair shop equipment
    "52",    # Measuring tools
    "56",    # Construction and building materials
    "63",    # Security / detection systems
    "65",    # Medical / dental / veterinary equipment
    "66",    # Instruments / lab equipment
    "67",    # Photographic / video equipment
    "71",    # Furniture
    "72",    # Household / commercial furnishings
    "73",    # Food prep / serving equipment
    "74",    # Office machines / business equipment
    "84",    # Clothing / individual equipment
    "95",    # Metal bars, sheets, shapes, etc.
]

EXPANDED_PSC_CODES = ["24", "37", "51", "54", "70"]
if os.environ.get("EXPANDED_EQUIPMENT_PSC", "0") == "1":
    PSC_CODES.extend(EXPANDED_PSC_CODES)

# Excluded PSC prefixes (pure services, heavy maintenance, construction, tiny hardware)
AVOID_PSC_CODES = [
    "J", "R", "S", "Y", "Z", "25", "28", "29", "30", "31",
    "47", "48", "53", "59", "596", "597", "599", "61", "6150", "62", "68",
]

# Targeted Set-Aside patterns (matched flexibly against code or description)
SET_ASIDE_PATTERNS = [
    r"\bSBA\b", r"\bSMALL BUSINESS\b", r"\bTOTAL SMALL\b", r"\bPARTIAL SMALL\b",
    r"\b8\(?A\)?\b", r"\bHUBZONE\b", r"\bSDVOSB\b", r"\bSERVICE-DISABLED\b",
    r"\bWOSB\b", r"\bWOMEN-OWNED\b", r"\bEDWOSB\b", r"\bECONOMICALLY DISADVANTAGED\b",
    r"\bISBEE\b", r"\bINDIAN-OWNED\b"
]

# Title Exclusions (focus strictly on services, install, heavy maintenance, construction)
HARD_EXCLUDE_TITLE_PATTERNS = [
    r"\bASSEMBL(Y|IES)\b",
    r"\bPARTS KIT\b",
    r"\bRE[- ]?KEY\b",
    r"\bRECONFIGURATION\b",
    r"\bREPAIR\b",
    r"\bMAINTENANCE\b",
    r"\bMAINT\b",
    r"\bOVERHAUL\b",
    r"\bREFURB(ISH|ISHMENT)?\b",
    r"\bINSTALLATION AND \b",
    r"\bINSTALLATION OF\b",
    r"\bINSTALLATION SERVICE\b",
    r"\bCALIBRATION\b",
    r"\bREMOVAL\b",
    r"\bDISPOSAL\b",
    r"\bDEMO(LITION)?\b",
    r"\bRENOVATION\b",
    r"\bCONSTRUCTION\b",
    r"\bSUPPORT SERVICES\b",
    r"\bTRAINING\b",
    r"\bSITE VISIT\b",
    r"\bINSPECTION\b",
    r"\bTESTING\b",
    r"\bCLEANING\b",
    r"\bPEST CONTROL\b",
    r"\bMOWING\b",
    r"\bLODGING\b",
    r"\bRENTAL\b",
    r"\bLEASE\b",
]

# High-Fit Title Keywords (give extra score boost)
GOOD_SUPPLY_TITLE_PATTERNS = [
    r"\bSUPPLY\b", r"\bSUPPLIES\b", r"\bDELIVER\b", r"\bDELIVERY\b",
    r"\bPURCHASE\b", r"\bPROCUREMENT\b", r"\bEQUIPMENT\b", r"\bMACHINE\b",
    r"\bSYSTEM\b", r"\bSYSTEMS\b", r"\bFURNITURE\b", r"\bCABINET\b",
    r"\bFREEZER\b", r"\bREFRIGERATOR\b", r"\bGENERATOR\b", r"\bBOOTS\b",
    r"\bHELMETS?\b", r"\bCART\b", r"\bTABLE\b", r"\bUNIT\b", r"\bUNITS\b"
]

BEST_SUPPLY_TITLE_PATTERNS = [
    r"\bPURCHASE AND DELIVERY\b",
    r"\bSUPPLY AND DELIVER\b",
    r"\bMATERIALS SUPPLY\b",
    r"\bEQUIPMENT PURCHASE\b",
    r"\bOFFICE FURNITURE\b",
    r"\bMEDICAL EQUIPMENT\b",
    r"\bLAB(ORATORY)? EQUIPMENT\b",
]

DLA_AGENCY_PATTERN = re.compile(r"DEFENSE LOGISTICS AGENCY|DLA\b", re.IGNORECASE)
DLA_SOLICITATION_PREFIXES = ("SPE", "SPR")

# API safety controls.
MAX_PAGES_PER_QUERY = int(os.environ.get("MAX_PAGES_PER_QUERY", "5"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "45"))
RESULTS_PER_PAGE = int(os.environ.get("RESULTS_PER_PAGE", "100"))
MAX_API_CALLS_PER_RUN = int(os.environ.get("MAX_API_CALLS_PER_RUN", "250"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.environ.get("SEEN_FILE", os.path.join(SCRIPT_DIR, "seen_notices.json"))
PDF_PATH = os.environ.get("PDF_PATH", os.path.join(SCRIPT_DIR, "sam_gov_low_hanging_opportunities.pdf"))
CSV_PATH = os.environ.get("CSV_PATH", os.path.join(SCRIPT_DIR, "sam_gov_scan_audit.csv"))

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
# HELPERS
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
    return [p for p in patterns if re.search(p, title, flags=re.IGNORECASE)]

def load_seen_state() -> dict[str, str]:
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {clean_text(nid): "" for nid in data if clean_text(nid)}
        if isinstance(data, dict):
            return {clean_text(k): clean_text(v) for k, v in data.items() if clean_text(k)}
    except (json.JSONDecodeError, OSError):
        pass
    return {}

def save_seen_state(seen_state: dict[str, str]) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(seen_state.items())), f, indent=2)

def get_set_aside_full_text(opp: dict[str, Any]) -> str:
    parts = [
        opp.get("typeOfSetAside"),
        opp.get("typeOfSetAsideDescription"),
        opp.get("setAsideCode"),
        opp.get("setAside"),
    ]
    return normalize_upper(" ".join([p for p in parts if p]))

def get_response_deadline(opp: dict[str, Any]) -> str:
    return clean_text(
        opp.get("responseDeadLine") or opp.get("responseDeadline") or opp.get("reponseDeadLine")
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
        parts = [p for p in [name, email, phone] if p]
        if parts:
            pieces.append(" / ".join(parts))
    return "; ".join(pieces)

def get_notice_signature(opp: dict[str, Any]) -> str:
    fields = [
        clean_text(opp.get("noticeId")),
        clean_text(opp.get("solicitationNumber")),
        clean_text(opp.get("title")),
        get_posted_date(opp),
        get_response_deadline(opp),
        clean_text(opp.get("classificationCode")),
        get_set_aside_full_text(opp),
    ]
    return "|".join(fields)


# ---------------------------------------------------------------------------
# FILTERS AND SCORING
# ---------------------------------------------------------------------------

def is_target_psc(opp: dict[str, Any]) -> bool:
    code = normalize_upper(opp.get("classificationCode"))
    if not code:
        return False
    if any(code.startswith(bad) for bad in AVOID_PSC_CODES):
        return False
    return any(code.startswith(prefix) for prefix in PSC_CODES)

def is_small_business_set_aside(opp: dict[str, Any]) -> bool:
    full_text = get_set_aside_full_text(opp)
    if not full_text:
        return False
    # If explicitly marked NO SET ASIDE or FULL AND OPEN, reject
    if "NO SET ASIDE" in full_text or "FULL AND OPEN" in full_text or full_text == "NBO":
        return False
    return any(re.search(pat, full_text, flags=re.IGNORECASE) for pat in SET_ASIDE_PATTERNS)

def is_dla_parts_noise(opp: dict[str, Any]) -> bool:
    agency = get_agency(opp)
    solicitation = normalize_upper(opp.get("solicitationNumber"))
    title = normalize_upper(opp.get("title"))

    if DLA_AGENCY_PATTERN.search(agency):
        return True
    if solicitation.startswith(DLA_SOLICITATION_PREFIXES):
        return True
    if "NSN" in title:
        return True
    return False

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

    if not is_small_business_set_aside(opp):
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason=f"Not a target small-business set-aside: '{get_set_aside_full_text(opp)}'.",
        )
    score += 2
    reasons.append("Eligible small-business set-aside.")

    if not is_target_psc(opp):
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason=f"PSC '{code}' is not in target supply list or excluded as noise.",
        )
    score += 2
    reasons.append(f"Target supply/equipment PSC {code}.")

    if is_dla_parts_noise(opp):
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason="DLA/SPE/SPR/NSN parts notice.",
        )

    hard_excluded, patterns = has_hard_title_exclusion(opp)
    if hard_excluded:
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason="Title indicates heavy service/repair/install: " + ", ".join(patterns[:3]),
        )

    # Bonus points for strong title keywords
    if title_matches_any(title, GOOD_SUPPLY_TITLE_PATTERNS):
        score += 2
        reasons.append("Supply/equipment keywords present in title.")

    if title_matches_any(title, BEST_SUPPLY_TITLE_PATTERNS):
        score += 2
        reasons.append("High-fit procurement phrase in title.")

    if solicitation and "Q" in solicitation:
        score += 1
        reasons.append("RFQ-style solicitation number.")

    hours = deadline_hours(opp)
    if hours is None:
        warnings.append("No parseable response deadline; check SAM.gov manually.")
    elif hours <= 0:
        return Evaluation(status="rejected", pursue_score=0, rejection_reason="Response deadline has passed.")
    elif hours <= MIN_HOURS_UNTIL_DEADLINE:
        return Evaluation(
            status="rejected",
            pursue_score=0,
            rejection_reason=f"Due too soon: {hours:.1f} hours remaining.",
        )
    elif hours >= 7 * 24:
        score += 2
        reasons.append("7+ days remaining until deadline.")
    else:
        score += 1
        reasons.append("Short deadline window; check lead times quickly.")

    if any(w in title for w in ["CUSTOM", "FABRICATE", "FABRICATION"]):
        warnings.append("Potential custom fabrication requirement.")
        score -= 1

    # Balanced Thresholds
    if score >= 6:
        status = "strong"
    elif score >= 3:
        status = "conditional"
    else:
        status = "rejected"

    rejection_reason = "" if status != "rejected" else f"Score ({score}) below threshold."
    return Evaluation(status=status, pursue_score=max(score, 0), reasons=reasons, warnings=warnings, rejection_reason=rejection_reason)

def sort_key_for_opportunity(opp: dict[str, Any]) -> tuple[float, int, str]:
    posted_dt = parse_date(get_posted_date(opp))
    posted_ts = posted_dt.timestamp() if posted_dt else 0.0
    eval_score = int(opp.get("pursue_score", 0) or 0)
    title = clean_text(opp.get("title"))
    return (-posted_ts, -eval_score, title)


# ---------------------------------------------------------------------------
# FETCH LOGIC
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
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            time.sleep(2 * attempt)

    if response is None:
        raise RuntimeError("Network failure reaching SAM.gov API.")
    if response.status_code == 429:
        raise RuntimeError("SAM.gov API rate limit reached.")

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

            while True:
                if api_calls >= MAX_API_CALLS_PER_RUN:
                    print(f"Reached API call cap ({MAX_API_CALLS_PER_RUN}).")
                    return all_records

                params = build_sam_params(posted_from, posted_to, notice_type, psc_code, offset)
                data = fetch_page(params)
                api_calls += 1

                records = data.get("opportunitiesData", []) or []
                total = int(data.get("totalRecords", 0) or 0)
                page_count += 1

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

                if not records or len(records) < RESULTS_PER_PAGE or page_count >= MAX_PAGES_PER_QUERY:
                    break

                offset += RESULTS_PER_PAGE
                time.sleep(0.2)

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

    print(f"Scanning SAM.gov ({posted_from_str} to {posted_to_str})...", flush=True)
    records = fetch_all_opportunities(posted_from_str, posted_to_str)
    print(f"Fetched {len(records)} raw record(s) from SAM.gov.", flush=True)

    for opp in records:
        notice_id = clean_text(opp.get("noticeId"))
        evaluation = evaluate_opportunity(opp)

        opp["pursue_status"] = evaluation.status
        opp["pursue_score"] = evaluation.pursue_score
        opp["pursue_reasons"] = "; ".join(evaluation.reasons)
        opp["pursue_warnings"] = "; ".join(evaluation.warnings)
        opp["rejection_reason"] = evaluation.rejection_reason

        signature = get_notice_signature(opp)
        already_seen = bool(notice_id and seen_state.get(notice_id) == signature)

        audit_rows.append(opp)

        if evaluation.status in {"strong", "conditional"}:
            all_passing_matches.append(opp)
            if DISABLE_SEEN_FILTER or not already_seen:
                digest_matches.append(opp)
            if notice_id:
                seen_state[notice_id] = signature

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
        "pursue_status", "pursue_score", "postedDate", "responseDeadLine",
        "title", "solicitationNumber", "noticeId", "classificationCode",
        "searched_psc_code", "searched_notice_type", "type", "set_aside_full",
        "agency", "poc", "link", "pursue_reasons", "pursue_warnings", "rejection_reason"
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for opp in audit_rows:
            writer.writerow({
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
                "set_aside_full": get_set_aside_full_text(opp),
                "agency": get_agency(opp),
                "poc": get_poc_summary(opp),
                "link": get_notice_link(opp),
                "pursue_reasons": clean_text(opp.get("pursue_reasons")),
                "pursue_warnings": clean_text(opp.get("pursue_warnings")),
                "rejection_reason": clean_text(opp.get("rejection_reason")),
            })

def get_pdf_items(digest_matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for opp in digest_matches:
        st = clean_text(opp.get("pursue_status"))
        if st == "strong" or (st == "conditional" and INCLUDE_CONDITIONAL_IN_PDF):
            items.append(opp)
    return items[:MAX_DIGEST_ITEMS]

def generate_plain_text_digest(digest_matches: list[dict[str, Any]], audit_rows: list[dict[str, Any]]) -> str:
    counts = summarize_counts(audit_rows)
    pdf_items = get_pdf_items(digest_matches)

    if not digest_matches:
        return (
            "SAM.gov delivery/supply scan completed.\n\n"
            "No new matching opportunities were found in this run.\n\n"
            f"Evaluated totals: Strong {counts.get('strong', 0)}, "
            f"Conditional {counts.get('conditional', 0)}, Rejected {counts.get('rejected', 0)}.\n"
        )

    lines = [
        "SAM.gov delivery/supply scan complete.",
        f"- Digest items: {len(pdf_items)}",
        f"- Strong RFQs: {counts.get('strong', 0)}",
        f"- Conditional RFQs: {counts.get('conditional', 0)}",
        f"- Rejected notices: {counts.get('rejected', 0)}",
        "",
    ]
    for idx, opp in enumerate(pdf_items, start=1):
        lines.extend([
            f"{idx}. [{clean_text(opp.get('pursue_status')).upper()} / Score {clean_text(opp.get('pursue_score'))}] {clean_text(opp.get('title'))}",
            f"   Solicitation: {clean_text(opp.get('solicitationNumber')) or 'N/A'}",
            f"   Posted: {get_posted_date(opp)} | Due: {get_response_deadline(opp)}",
            f"   Link: {get_notice_link(opp)}",
            ""
        ])
    return "\n".join(lines)

def add_opportunity_to_story(story: list[Any], styles: dict[str, Any], opp: dict[str, Any]) -> None:
    title = escape(clean_text(opp.get("title")) or "Untitled")
    solnum = escape(clean_text(opp.get("solicitationNumber")) or "N/A")
    agency = escape(get_agency(opp) or "Unknown")
    posted = escape(get_posted_date(opp) or "N/A")
    deadline = escape(get_response_deadline(opp) or "N/A")
    score = escape(clean_text(opp.get("pursue_score")) or "0")
    status = escape(clean_text(opp.get("pursue_status")).upper())
    link = escape(get_notice_link(opp))

    story.append(Paragraph(f"[{status} | Score {score}] {title}", styles["Heading3"]))
    story.append(Paragraph(f"<b>Solicitation:</b> {solnum} &nbsp;|&nbsp; <b>Posted:</b> {posted} &nbsp;|&nbsp; <b>Due:</b> {deadline}", styles["Normal"]))
    story.append(Paragraph(f"<b>Agency:</b> {agency}", styles["Normal"]))
    story.append(Paragraph(f"<b>Why it surfaced:</b> {escape(clean_text(opp.get('pursue_reasons')))}", styles["Normal"]))
    if link:
        story.append(Paragraph(f'<link href="{link}">{link}</link>', styles["Normal"]))
    story.append(Spacer(1, 0.1 * inch))
    story.append(HRFlowable(width="100%", color="#cccccc"))
    story.append(Spacer(1, 0.1 * inch))

def generate_pdf(digest_matches: list[dict[str, Any]], audit_rows: list[dict[str, Any]], filepath: str) -> None:
    doc = SimpleDocTemplate(filepath, pagesize=letter, margin=0.5*inch)
    styles = getSampleStyleSheet()
    story: list[Any] = []
    pdf_items = get_pdf_items(digest_matches)

    story.append(Paragraph("SAM.gov Delivery & Supply Bids", styles["Title"]))
    story.append(Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    if not pdf_items:
        story.append(Paragraph("No matching opportunities found in this scan.", styles["Heading2"]))
    else:
        for opp in pdf_items:
            add_opportunity_to_story(story, styles, opp)

    doc.build(story)

def send_email_with_outputs(digest_matches: list[dict[str, Any]], audit_rows: list[dict[str, Any]]) -> None:
    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        print("Email credentials missing. Skipping email send.")
        return

    generate_pdf(digest_matches, audit_rows, PDF_PATH)
    generate_csv(audit_rows, CSV_PATH)

    counts = summarize_counts(audit_rows)
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"SAM.gov Bids: {counts.get('strong', 0)} Strong, {counts.get('conditional', 0)} Conditional"

    msg.attach(MIMEText(generate_plain_text_digest(digest_matches, audit_rows), "plain"))

    for path, filename in [(PDF_PATH, "sam_gov_opportunities.pdf"), (CSV_PATH, "sam_gov_scan_audit.csv")]:
        if os.path.exists(path):
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            msg.attach(part)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    print(f"Scan digest sent to {EMAIL_TO}.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    if API_KEY == "PUT_YOUR_KEY_HERE":
        raise SystemExit("Please set the SAM_API_KEY environment variable.")

    digest_matches, all_passing, audit_rows = run_scan()
    counts = summarize_counts(audit_rows)

    print(
        f"Scan Summary: {counts.get('strong')} Strong, "
        f"{counts.get('conditional')} Conditional, "
        f"{counts.get('rejected')} Rejected."
    )

    if ALWAYS_EMAIL_SCAN_RESULTS or digest_matches:
        send_email_with_outputs(digest_matches, audit_rows)

if __name__ == "__main__":
    main()
