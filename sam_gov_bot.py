#!/usr/bin/env python3
"""
sam_gov_low_hanging_bot.py

Scans the official SAM.gov Contract Opportunities Public API for active RFQs,
scores recent "low-hanging-fruit" supply/equipment opportunities, and emails
a PDF digest plus a CSV audit trail.

This rewrite is designed to avoid the main failure modes in the prior script:

1. No PSC-by-PSC querying. It fetches the full recent opportunity pool once.
2. Correct pagination: limit and offset advance by the number of records
   actually returned.
3. Four-day overlapping lookback by default.
4. Product PSCs are ranked rather than restricted to a narrow whitelist.
5. Installation/training/service terms lower an opportunity's score instead of
   automatically hiding every otherwise viable equipment purchase.
6. Every fetched opportunity is written to an audit CSV with the exact reason
   it was included, downgraded, or rejected.
7. Amendments and material changes can be emailed again because the bot stores
   a fingerprint, not only a Notice ID.
8. Seen-state is updated only after a successful email, unless you explicitly
   choose otherwise.

OFFICIAL API
------------
https://api.sam.gov/opportunities/v2/search

REQUIRED ENVIRONMENT VARIABLE
-----------------------------
SAM_API_KEY

OPTIONAL EMAIL ENVIRONMENT VARIABLES
------------------------------------
SMTP_SERVER   default: smtp.gmail.com
SMTP_PORT     default: 587
SMTP_USER
SMTP_PASS     Gmail users should use an App Password
EMAIL_TO      default: SMTP_USER

OPTIONAL BOT ENVIRONMENT VARIABLES
----------------------------------
LOOKBACK_DAYS                    default: 4
MIN_HOURS_UNTIL_DEADLINE         default: 48
ALLOW_UNRESTRICTED               default: true
MIN_SCORE_STRONG                 default: 8
MIN_SCORE_CONDITIONAL            default: 5
MAX_EMAIL_ITEMS                  default: 50
MARK_SEEN_WITHOUT_EMAIL          default: false
SEND_ZERO_RESULT_EMAIL           default: false

INSTALL
-------
python -m pip install requests python-dateutil reportlab

RUN
---
python sam_gov_low_hanging_bot.py

Useful options:
python sam_gov_low_hanging_bot.py --dry-run
python sam_gov_low_hanging_bot.py --reset-seen
python sam_gov_low_hanging_bot.py --print-top 20
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import smtplib
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.sax.saxutils import escape

import requests
from dateutil import parser as date_parser
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

API_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_API_KEY = os.environ.get("SAM_API_KEY", "").strip()

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "sam_seen_rfqs_v3.json"
PDF_PATH = SCRIPT_DIR / "sam_gov_low_hanging_opportunities.pdf"
AUDIT_CSV_PATH = SCRIPT_DIR / "sam_gov_scan_audit.csv"

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER).strip()


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise SystemExit(f"{name} must be an integer; received {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}; received {value}.")
    return value


LOOKBACK_DAYS = env_int("LOOKBACK_DAYS", 4, minimum=1)
MIN_HOURS_UNTIL_DEADLINE = env_int(
    "MIN_HOURS_UNTIL_DEADLINE", 48, minimum=0
)
ALLOW_UNRESTRICTED = env_bool("ALLOW_UNRESTRICTED", True)
MARK_SEEN_WITHOUT_EMAIL = env_bool("MARK_SEEN_WITHOUT_EMAIL", False)
SEND_ZERO_RESULT_EMAIL = env_bool("SEND_ZERO_RESULT_EMAIL", False)

MIN_SCORE_STRONG = env_int("MIN_SCORE_STRONG", 8)
MIN_SCORE_CONDITIONAL = env_int("MIN_SCORE_CONDITIONAL", 5)
MAX_EMAIL_ITEMS = env_int("MAX_EMAIL_ITEMS", 50, minimum=1)

RESULTS_PER_PAGE = 1000
MAX_PAGES = 25
MAX_RETRIES = 4
REQUEST_TIMEOUT_SECONDS = 60

# RFQ discovery is limited to active Solicitation and Combined
# Synopsis/Solicitation notices. Sources Sought and Presolicitations are
# intentionally excluded.
NOTICE_TYPES: tuple[str, ...] = ("k", "o")

# New United Partners is currently treated as eligible for ordinary small
# business set-asides only. Add a code here only after the corresponding
# certification is active.
ELIGIBLE_SET_ASIDE_CODES = {
    "SBA",  # Total Small Business Set-Aside
    "SBP",  # Partial Small Business Set-Aside
}

# These are restricted programs. The bot does not surface them as pursuits
# unless you add the applicable code to ELIGIBLE_SET_ASIDE_CODES.
RESTRICTED_SET_ASIDE_CODES = {
    "8A",
    "8AN",
    "HZC",
    "HZS",
    "SDVOSBC",
    "SDVOSBS",
    "WOSB",
    "WOSBSS",
    "EDWOSB",
    "EDWOSBSS",
    "VSA",
    "VSS",
}

# Product/service codes beginning with a number are generally supply/product
# PSCs. Alphabetic PSCs are usually services. These are the strongest service,
# construction, repair, and lease prefixes to reject.
HARD_SERVICE_PSC_PREFIXES = {
    "A",  # R&D services
    "B",  # Special studies/analysis
    "C",  # Architect/engineering
    "D",  # IT/telecom services
    "E",  # Purchase of structures/facilities
    "F",  # Natural resources services
    "G",  # Social services
    "H",  # Quality control/testing/inspection
    "J",  # Maintenance/repair/rebuild of equipment
    "K",  # Modification of equipment
    "L",  # Technical representative services
    "M",  # Operation of government-owned facilities
    "N",  # Installation of equipment
    "P",  # Salvage
    "Q",  # Medical services
    "R",  # Professional/admin/management support
    "S",  # Utilities/housekeeping
    "T",  # Photographic/mapping/printing services
    "U",  # Education/training
    "V",  # Transportation/travel/relocation
    "W",  # Lease/rental of equipment
    "X",  # Lease/rental of facilities
    "Y",  # Construction of structures/facilities
    "Z",  # Repair/alteration of real property
}

# Strong product/supply language. These terms are intentionally broad because
# the scoring stage, not a narrow whitelist, decides what reaches the digest.
PRODUCT_TERMS = {
    "SUPPLY",
    "SUPPLIES",
    "EQUIPMENT",
    "MACHINE",
    "MACHINERY",
    "APPLIANCE",
    "FURNITURE",
    "TOOLS",
    "PART",
    "PARTS",
    "SPARES",
    "COMPONENT",
    "COMPONENTS",
    "ASSEMBLY",
    "ASSEMBLIES",
    "VALVE",
    "CYLINDER",
    "PUMP",
    "MOTOR",
    "SOLENOID",
    "SWITCH",
    "SENSOR",
    "BATTERY",
    "GENERATOR",
    "OVEN",
    "FURNACE",
    "MOWER",
    "TRACTOR",
    "WRAPPER",
    "BALER",
    "CHAIR",
    "TABLE",
    "CABINET",
    "PRINTER",
    "MONITOR",
    "SERVER",
    "LAPTOP",
    "MEDICAL",
    "LABORATORY",
    "REPLACEMENT",
    "PURCHASE",
    "PROCUREMENT",
    "DELIVERY",
    "FURNISH",
}

# Terms that often indicate extra operational burden. They reduce the score
# but do not automatically hide the opportunity.
BURDEN_TERMS: dict[str, int] = {
    "INSTALLATION": -2,
    "INSTALL": -2,
    "COMMISSIONING": -2,
    "TRAINING": -2,
    "SITE VISIT": -2,
    "DESIGN": -2,
    "ENGINEERING": -2,
    "CUSTOM FABRICATION": -3,
    "FABRICATION": -2,
    "TURNKEY": -2,
    "REMOVAL": -1,
    "DISPOSAL": -2,
    "MAINTENANCE": -3,
    "SERVICE": -2,
    "SERVICES": -2,
    "REPAIR": -3,
    "OVERHAUL": -3,
    "REBUILD": -3,
    "BASE YEAR": -2,
    "OPTION YEAR": -2,
    "OPTIONS": -1,
    "SUBSCRIPTION": -2,
    "LICENSE": -1,
    "RENTAL": -3,
    "LEASE": -3,
}

# These phrases are usually clear non-pursuits for a reseller.
HARD_REJECT_PHRASES = {
    "INTENT TO SOLE SOURCE",
    "SOLE SOURCE NOTICE",
    "NOTICE OF INTENT",
    "ONLY ONE RESPONSIBLE SOURCE",
    "CONSTRUCTION SERVICES",
    "JANITORIAL SERVICES",
    "CUSTODIAL SERVICES",
    "PROFESSIONAL SERVICES",
    "ARCHITECT-ENGINEER",
    "A-E SERVICES",
    "ROOF REPLACEMENT",
    "BUILDING RENOVATION",
    "DEMOLITION",
    "HAZARDOUS WASTE DISPOSAL",
}

# These are risks, not automatic rejections.
RISK_TERMS: dict[str, str] = {
    "AUTHORIZED DISTRIBUTOR": "May require manufacturer/dealer authorization.",
    "AUTHORIZED RESELLER": "May require manufacturer/reseller authorization.",
    "TRACEABILITY": "May require OEM traceability documentation.",
    "CERTIFICATE OF CONFORMANCE": "May require a certificate of conformance.",
    "MIL-STD-129": "Military marking requirements may add cost.",
    "MIL-STD-2073": "Military preservation/packaging may add cost.",
    "TAA": "Confirm Trade Agreements Act compliance.",
    "COUNTRY OF ORIGIN": "Confirm country of origin.",
    "BUY AMERICAN": "Confirm Buy American compliance.",
    "BRAND NAME ONLY": "Exact-brand sourcing may be manufacturer controlled.",
    "NO SUBSTITUTIONS": "Exact item is required; supplier availability is the gate.",
    "SITE VISIT": "A site visit may be required or encouraged.",
}


# ---------------------------------------------------------------------------
# DATA STRUCTURES
# ---------------------------------------------------------------------------

@dataclass
class Evaluation:
    opportunity: dict[str, Any]
    notice_id: str
    solicitation_number: str
    title: str
    agency: str
    notice_type: str
    posted_date: str
    deadline_text: str
    deadline_dt: datetime | None
    hours_until_deadline: float | None
    set_aside_code: str
    set_aside_description: str
    psc: str
    link: str
    score: int = 0
    bucket: str = "rejected"
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    fingerprint: str = ""
    change_status: str = "NEW"

    @property
    def is_actionable(self) -> bool:
        return self.bucket in {"strong", "conditional"}

    @property
    def sort_deadline(self) -> datetime:
        return self.deadline_dt or datetime.max.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# FIELD NORMALIZATION
# ---------------------------------------------------------------------------

def first_value(data: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def get_deadline_text(opp: Mapping[str, Any]) -> str:
    # SAM documentation and real responses have used more than one spelling.
    return first_value(
        opp,
        "responseDeadLine",
        "responseDeadline",
        "reponseDeadLine",
        "response_deadline",
    )


def get_set_aside_code(opp: Mapping[str, Any]) -> str:
    code = first_value(
        opp,
        "typeOfSetAside",
        "setAsideCode",
        "setasideCode",
    ).upper()

    description = get_set_aside_description(opp).upper()
    if not code:
        if "TOTAL SMALL BUSINESS" in description:
            return "SBA"
        if "PARTIAL SMALL BUSINESS" in description:
            return "SBP"
    return code


def get_set_aside_description(opp: Mapping[str, Any]) -> str:
    return first_value(
        opp,
        "typeOfSetAsideDescription",
        "setAside",
        "setAsideDescription",
    )


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_text_blob(opp: Mapping[str, Any]) -> str:
    parts: list[str] = [
        first_value(opp, "title"),
        first_value(opp, "description"),
        first_value(opp, "additionalInfo"),
        first_value(opp, "solicitationNumber"),
        get_set_aside_description(opp),
        first_value(opp, "classificationCode"),
    ]
    return " ".join(part for part in parts if part).upper()


def get_notice_type_code(opp: Mapping[str, Any]) -> str:
    value = first_value(opp, "type", "baseType").lower()
    mapping = {
        "presolicitation": "p",
        "pre solicitation": "p",
        "combined synopsis/solicitation": "k",
        "combined synopsis solicitation": "k",
        "solicitation": "o",
        "sources sought": "r",
        "source sought": "r",
    }
    return mapping.get(value, value[:1] if value else "")


def notice_type_label(code_or_text: str) -> str:
    value = code_or_text.strip()
    labels = {
        "p": "Presolicitation",
        "k": "Combined Synopsis/Solicitation",
        "o": "Solicitation",
        "r": "Sources Sought",
    }
    return labels.get(value.lower(), value or "Unknown")


def is_active_opportunity(opp: Mapping[str, Any]) -> bool:
    active = first_value(opp, "active", "status").strip().lower()
    if not active:
        return True
    return active in {"yes", "y", "true", "1", "active", "published"}


def public_sam_link(opp: Mapping[str, Any]) -> str:
    notice_id = first_value(opp, "noticeId")
    if notice_id:
        return f"https://sam.gov/opp/{notice_id}/view"
    return first_value(opp, "uiLink", "additionalInfoLink")


def opportunity_fingerprint(opp: Mapping[str, Any]) -> str:
    payload = {
        "noticeId": first_value(opp, "noticeId"),
        "title": first_value(opp, "title"),
        "solicitationNumber": first_value(opp, "solicitationNumber"),
        "postedDate": first_value(opp, "postedDate"),
        "deadline": get_deadline_text(opp),
        "type": first_value(opp, "type", "baseType"),
        "setAsideCode": get_set_aside_code(opp),
        "setAsideDescription": get_set_aside_description(opp),
        "psc": first_value(opp, "classificationCode"),
        "active": first_value(opp, "active", "status"),
        "archiveDate": first_value(opp, "archiveDate"),
        "resourceLinks": opp.get("resourceLinks") or [],
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"version": 2, "records": {}}

    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(
            f"Warning: could not read {STATE_FILE.name}; starting with empty state.",
            file=sys.stderr,
        )
        return {"version": 2, "records": {}}

    if isinstance(raw, dict) and isinstance(raw.get("records"), dict):
        return raw

    # Safe migration for the old list-of-Notice-IDs format.
    if isinstance(raw, list):
        return {
            "version": 2,
            "records": {
                str(notice_id): {
                    "fingerprint": "legacy",
                    "last_emailed_utc": "",
                }
                for notice_id in raw
            },
        }

    return {"version": 2, "records": {}}


def determine_change_status(evaluation: Evaluation, state: Mapping[str, Any]) -> str:
    record = state.get("records", {}).get(evaluation.notice_id)
    if not record:
        return "NEW"
    prior_fingerprint = str(record.get("fingerprint", ""))
    if prior_fingerprint == "legacy":
        return "LEGACY-SEEN"
    if prior_fingerprint != evaluation.fingerprint:
        return "UPDATED"
    return "UNCHANGED"


def save_state(state: Mapping[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def mark_emailed(
    state: dict[str, Any],
    evaluations: Iterable[Evaluation],
) -> None:
    records = state.setdefault("records", {})
    now_text = datetime.now(timezone.utc).isoformat()
    for evaluation in evaluations:
        records[evaluation.notice_id] = {
            "fingerprint": evaluation.fingerprint,
            "last_emailed_utc": now_text,
            "title": evaluation.title,
            "solicitation_number": evaluation.solicitation_number,
        }

    # Prevent unbounded state growth.
    if len(records) > 10000:
        sortable: list[tuple[str, str]] = []
        for notice_id, record in records.items():
            sortable.append((str(record.get("last_emailed_utc", "")), notice_id))
        sortable.sort(reverse=True)
        keep_ids = {notice_id for _, notice_id in sortable[:7500]}
        state["records"] = {
            notice_id: record
            for notice_id, record in records.items()
            if notice_id in keep_ids
        }


# ---------------------------------------------------------------------------
# SAM.GOV API
# ---------------------------------------------------------------------------

def request_page(
    session: requests.Session,
    posted_from: str,
    posted_to: str,
    offset: int,
) -> dict[str, Any]:
    params: list[tuple[str, str]] = [
        ("api_key", SAM_API_KEY),
        ("postedFrom", posted_from),
        ("postedTo", posted_to),
        ("limit", str(RESULTS_PER_PAGE)),
        ("offset", str(offset)),
    ]
    params.extend(("ptype", notice_type) for notice_type in NOTICE_TYPES)

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                API_URL,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            if response.status_code == 404:
                return {"totalRecords": 0, "opportunitiesData": []}

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait_seconds = (
                    int(retry_after)
                    if retry_after and retry_after.isdigit()
                    else 10 * attempt
                )
                print(
                    f"Rate limited by SAM.gov. Waiting {wait_seconds} seconds...",
                    flush=True,
                )
                time.sleep(wait_seconds)
                continue

            if 500 <= response.status_code < 600:
                response.raise_for_status()

            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("SAM.gov returned a non-object JSON response.")
            return data

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError,
            requests.exceptions.JSONDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                break
            wait_seconds = 3 * attempt
            print(
                f"SAM.gov request failed on attempt {attempt}/{MAX_RETRIES}: "
                f"{exc}. Retrying in {wait_seconds} seconds...",
                flush=True,
            )
            time.sleep(wait_seconds)

    raise RuntimeError(
        f"SAM.gov request failed after {MAX_RETRIES} attempts."
    ) from last_error


def fetch_recent_opportunities() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    posted_from = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    posted_to = now.strftime("%m/%d/%Y")

    print(
        f"Fetching SAM.gov notices posted {posted_from} through {posted_to} "
        f"for notice types {', '.join(NOTICE_TYPES)}...",
        flush=True,
    )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "NewUnitedPartners-SAM-Low-Hanging-Bot/2.0 "
                "(contract opportunity monitoring)"
            )
        }
    )

    all_records: list[dict[str, Any]] = []
    offset = 0

    for page_number in range(1, MAX_PAGES + 1):
        data = request_page(session, posted_from, posted_to, offset)
        raw_records = data.get("opportunitiesData") or []
        records = [record for record in raw_records if isinstance(record, dict)]
        total_records = int(data.get("totalRecords") or 0)

        all_records.extend(records)
        print(
            f"Page {page_number}: received {len(records)} records; "
            f"{len(all_records)} of {total_records} collected.",
            flush=True,
        )

        if not records:
            break

        # Correct pagination: advance by what the API actually returned.
        offset += len(records)

        if offset >= total_records or len(records) < RESULTS_PER_PAGE:
            break

        if page_number == MAX_PAGES:
            print(
                f"Warning: reached MAX_PAGES={MAX_PAGES}. "
                f"{max(total_records - len(all_records), 0)} records may remain.",
                file=sys.stderr,
            )
        time.sleep(0.5)

    # Deduplicate Notice IDs. If a duplicate appears, keep the record with the
    # latest posted date or, if tied, the later occurrence.
    by_notice_id: dict[str, dict[str, Any]] = {}
    no_id_records: list[dict[str, Any]] = []

    for record in all_records:
        notice_id = first_value(record, "noticeId")
        if not notice_id:
            no_id_records.append(record)
            continue

        prior = by_notice_id.get(notice_id)
        if prior is None:
            by_notice_id[notice_id] = record
            continue

        prior_date = parse_datetime(first_value(prior, "postedDate"))
        current_date = parse_datetime(first_value(record, "postedDate"))
        if current_date and (not prior_date or current_date >= prior_date):
            by_notice_id[notice_id] = record

    deduped = list(by_notice_id.values()) + no_id_records
    print(
        f"Fetched {len(all_records)} raw records; "
        f"{len(deduped)} remain after deduplication.",
        flush=True,
    )
    return deduped


# ---------------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------------

def phrase_present(text: str, phrase: str) -> bool:
    pattern = r"(?<![A-Z0-9])" + re.escape(phrase) + r"(?![A-Z0-9])"
    return re.search(pattern, text) is not None


def contains_any(text: str, phrases: Iterable[str]) -> list[str]:
    return sorted({phrase for phrase in phrases if phrase_present(text, phrase)})



RFQ_POSITIVE_PHRASES = {
    "REQUEST FOR QUOTE",
    "REQUEST FOR QUOTATION",
    "REQUEST FOR QUOTATIONS",
    "RFQ",
    "QUOTE DUE",
    "QUOTATION DUE",
    "SUBMIT QUOTE",
    "SUBMIT A QUOTE",
    "SUBMIT QUOTATION",
    "SIMPLIFIED ACQUISITION",
    "FAR PART 13",
}

NON_RFQ_PHRASES = {
    "REQUEST FOR PROPOSAL",
    "REQUEST FOR PROPOSALS",
    "RFP",
    "INVITATION FOR BID",
    "INVITATION FOR BIDS",
    "IFB",
    "SEALED BID",
    "BROAD AGENCY ANNOUNCEMENT",
}


def rfq_evidence(text: str, solicitation_number: str) -> tuple[list[str], list[str]]:
    positive = contains_any(text, RFQ_POSITIVE_PHRASES)
    negative = contains_any(text, NON_RFQ_PHRASES)

    normalized_number = re.sub(r"[^A-Z0-9]", "", solicitation_number.upper())
    if "Q" in normalized_number and "Solicitation number contains Q" not in positive:
        positive.append("Solicitation number contains Q")

    return positive, negative


def exact_item_signals(text: str) -> list[str]:
    signals: list[str] = []

    nsn_patterns = [
        r"\bNSN\b",
        r"\b\d{4}-\d{2}-\d{3}-\d{4}\b",
    ]
    if any(re.search(pattern, text) for pattern in nsn_patterns):
        signals.append("NSN or stock-number-driven requirement")

    part_patterns = [
        r"\bPART\s*(?:NO\.?|NUMBER|#)?\s*[:\-]?\s*[A-Z0-9][A-Z0-9./_-]{2,}",
        r"\bP/N\s*[:\-]?\s*[A-Z0-9][A-Z0-9./_-]{2,}",
        r"\bMODEL\s*(?:NO\.?|NUMBER|#)?\s*[:\-]?\s*[A-Z0-9][A-Z0-9./_-]{2,}",
    ]
    if any(re.search(pattern, text) for pattern in part_patterns):
        signals.append("Manufacturer part/model number appears available")

    if "BRAND NAME OR EQUAL" in text or "BRAND-NAME-OR-EQUAL" in text:
        signals.append("Brand-name-or-equal competition")

    if re.search(r"\b(?:QTY|QUANTITY)\b", text) or re.search(
        r"\b\d+\s*(?:EA|EACH|UNITS?|PCS|PIECES)\b", text
    ):
        signals.append("Defined quantity appears available")

    return signals


def evaluate_opportunity(
    opp: dict[str, Any],
    now: datetime,
    state: Mapping[str, Any],
) -> Evaluation:
    notice_id = first_value(opp, "noticeId")
    title = first_value(opp, "title", default="Untitled")
    solicitation_number = first_value(opp, "solicitationNumber")
    agency = first_value(
        opp,
        "fullParentPathName",
        "organizationName",
        "department",
        default="Unknown agency",
    )
    notice_type_code = get_notice_type_code(opp)
    notice_type = notice_type_label(notice_type_code)
    posted_date = first_value(opp, "postedDate")
    deadline_text = get_deadline_text(opp)
    deadline_dt = parse_datetime(deadline_text)
    hours_until_deadline = (
        (deadline_dt - now).total_seconds() / 3600
        if deadline_dt
        else None
    )
    set_aside_code = get_set_aside_code(opp)
    set_aside_description = get_set_aside_description(opp)
    psc = first_value(opp, "classificationCode").upper()
    link = public_sam_link(opp)
    text = get_text_blob(opp)
    positive_rfq_signals, non_rfq_signals = rfq_evidence(
        text,
        solicitation_number,
    )

    evaluation = Evaluation(
        opportunity=opp,
        notice_id=notice_id,
        solicitation_number=solicitation_number,
        title=title,
        agency=agency,
        notice_type=notice_type,
        posted_date=posted_date,
        deadline_text=deadline_text,
        deadline_dt=deadline_dt,
        hours_until_deadline=hours_until_deadline,
        set_aside_code=set_aside_code,
        set_aside_description=set_aside_description,
        psc=psc,
        link=link,
    )
    evaluation.fingerprint = opportunity_fingerprint(opp)

    if notice_type_code not in {"o", "k"}:
        evaluation.rejection_reasons.append(
            f"Notice type {notice_type or 'unknown'} is not an RFQ-stage solicitation."
        )

    if non_rfq_signals:
        evaluation.rejection_reasons.append(
            "Explicit non-RFQ procurement method: "
            + ", ".join(non_rfq_signals)
            + "."
        )
    elif positive_rfq_signals:
        evaluation.score += 2
        evaluation.reasons.append(
            "RFQ indicators: " + ", ".join(positive_rfq_signals[:4]) + "."
        )
    else:
        evaluation.rejection_reasons.append(
            "The notice is not clearly identified as a request for quote/quotation."
        )

    if not notice_id:
        evaluation.rejection_reasons.append("Missing Notice ID.")

    if not is_active_opportunity(opp):
        evaluation.rejection_reasons.append("Opportunity is not active.")

    if deadline_dt and hours_until_deadline is not None:
        if hours_until_deadline <= 0:
            evaluation.rejection_reasons.append("Response deadline has passed.")
        elif hours_until_deadline < MIN_HOURS_UNTIL_DEADLINE:
            evaluation.rejection_reasons.append(
                f"Only {hours_until_deadline:.1f} hours remain; "
                f"minimum is {MIN_HOURS_UNTIL_DEADLINE}."
            )
        elif hours_until_deadline >= 14 * 24:
            evaluation.score += 2
            evaluation.reasons.append("At least 14 days remain.")
        elif hours_until_deadline >= 7 * 24:
            evaluation.score += 1
            evaluation.reasons.append("At least 7 days remain.")
        elif hours_until_deadline < 72:
            evaluation.score -= 1
            evaluation.warnings.append("Less than 3 days remain.")
    else:
        evaluation.warnings.append("No reliable response deadline was found.")

    # Eligibility / competition.
    unrestricted = not set_aside_code and (
        not set_aside_description
        or "NO SET ASIDE" in set_aside_description.upper()
        or "UNRESTRICTED" in set_aside_description.upper()
    )

    if set_aside_code in ELIGIBLE_SET_ASIDE_CODES:
        evaluation.score += 3
        evaluation.reasons.append("Eligible small-business set-aside.")
    elif set_aside_code in RESTRICTED_SET_ASIDE_CODES:
        evaluation.rejection_reasons.append(
            f"Restricted set-aside {set_aside_code} is not currently eligible."
        )
    elif unrestricted:
        if ALLOW_UNRESTRICTED:
            evaluation.score -= 1
            evaluation.warnings.append(
                "Unrestricted opportunity; expect broader competition."
            )
        else:
            evaluation.rejection_reasons.append(
                "Unrestricted opportunities are disabled by configuration."
            )
    elif set_aside_code:
        evaluation.warnings.append(
            f"Unrecognized set-aside code {set_aside_code}; verify eligibility."
        )
    else:
        evaluation.warnings.append("Set-aside status is unclear.")

    # PSC / product classification.
    if psc and psc[0].isdigit():
        evaluation.score += 2
        evaluation.reasons.append(f"Numeric product PSC {psc}.")
    elif psc and psc[0] in HARD_SERVICE_PSC_PREFIXES:
        evaluation.rejection_reasons.append(
            f"Service/construction/lease PSC {psc}."
        )
    elif not psc:
        evaluation.warnings.append("PSC is missing; product fit is title-dependent.")

    hard_phrases = contains_any(text, HARD_REJECT_PHRASES)
    if hard_phrases:
        evaluation.rejection_reasons.append(
            "Clear non-reseller scope: " + ", ".join(hard_phrases) + "."
        )

    # Avoid treating "repair parts" as a repair service.
    repair_parts_context = any(
        phrase in text
        for phrase in (
            "REPAIR PART",
            "REPAIR KIT",
            "SPARE PART",
            "REPLACEMENT PART",
            "PARTS KIT",
        )
    )

    burden_hits: list[tuple[str, int]] = []
    for phrase, penalty in BURDEN_TERMS.items():
        if phrase in text:
            if phrase in {"REPAIR", "SERVICE", "SERVICES"} and repair_parts_context:
                continue
            burden_hits.append((phrase, penalty))

    # Apply each burden category once.
    applied_burden_categories: set[str] = set()
    for phrase, penalty in burden_hits:
        category = phrase.split()[0]
        if category in applied_burden_categories:
            continue
        applied_burden_categories.add(category)
        evaluation.score += penalty
        evaluation.warnings.append(
            f"Scope may include {phrase.lower()} ({penalty} points)."
        )

    product_hits = contains_any(text, PRODUCT_TERMS)
    if product_hits:
        evaluation.score += 2
        evaluation.reasons.append(
            "Product/supply language: " + ", ".join(product_hits[:6]) + "."
        )
    elif not psc or not psc[0].isdigit():
        evaluation.rejection_reasons.append(
            "No convincing product/supply signal was found."
        )

    item_signals = exact_item_signals(text)
    for signal in item_signals:
        if signal.startswith("NSN"):
            evaluation.score += 3
        elif signal.startswith("Manufacturer"):
            evaluation.score += 2
        else:
            evaluation.score += 1
        evaluation.reasons.append(signal + ".")

    if any(
        phrase in text
        for phrase in (
            "FOB DESTINATION",
            "DELIVERY ONLY",
            "SUPPLY AND DELIVERY",
            "FURNISH AND DELIVER",
        )
    ):
        evaluation.score += 1
        evaluation.reasons.append("Delivery-focused scope is indicated.")

    if notice_type_code in {"o", "k"}:
        evaluation.score += 1
        evaluation.reasons.append("Open RFQ-stage solicitation.")

    for risk_term, warning in RISK_TERMS.items():
        if risk_term in text and warning not in evaluation.warnings:
            evaluation.warnings.append(warning)

    # More than one substantial burden is a meaningful concern, but still not
    # always a rejection. This keeps equipment with minor setup visible.
    substantial_burdens = {
        phrase
        for phrase, penalty in burden_hits
        if penalty <= -2
    }
    if len(substantial_burdens) >= 3:
        evaluation.score -= 2
        evaluation.warnings.append(
            "Multiple operational burdens suggest this may not be low hanging fruit."
        )

    if evaluation.rejection_reasons:
        evaluation.bucket = "rejected"
    elif evaluation.score >= MIN_SCORE_STRONG:
        evaluation.bucket = "strong"
    elif evaluation.score >= MIN_SCORE_CONDITIONAL:
        evaluation.bucket = "conditional"
    else:
        evaluation.bucket = "rejected"
        evaluation.rejection_reasons.append(
            f"Score {evaluation.score} is below "
            f"{MIN_SCORE_CONDITIONAL}."
        )

    evaluation.change_status = determine_change_status(evaluation, state)
    return evaluation


def evaluate_all(
    opportunities: Sequence[dict[str, Any]],
    state: Mapping[str, Any],
) -> list[Evaluation]:
    now = datetime.now(timezone.utc)
    evaluations = [
        evaluate_opportunity(opp, now=now, state=state)
        for opp in opportunities
    ]
    evaluations.sort(
        key=lambda item: (
            {"strong": 0, "conditional": 1, "rejected": 2}[
                item.bucket
            ],
            -item.score,
            item.sort_deadline,
            item.title.lower(),
        )
    )
    return evaluations


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def join_text(items: Iterable[str]) -> str:
    return " | ".join(item for item in items if item)


def write_audit_csv(evaluations: Sequence[Evaluation]) -> None:
    fields = [
        "bucket",
        "change_status",
        "score",
        "notice_id",
        "solicitation_number",
        "title",
        "agency",
        "notice_type",
        "posted_date",
        "deadline",
        "hours_until_deadline",
        "set_aside_code",
        "set_aside_description",
        "psc",
        "reasons",
        "warnings",
        "rejection_reasons",
        "link",
    ]

    with AUDIT_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in evaluations:
            writer.writerow(
                {
                    "bucket": item.bucket,
                    "change_status": item.change_status,
                    "score": item.score,
                    "notice_id": item.notice_id,
                    "solicitation_number": item.solicitation_number,
                    "title": item.title,
                    "agency": item.agency,
                    "notice_type": item.notice_type,
                    "posted_date": item.posted_date,
                    "deadline": item.deadline_text,
                    "hours_until_deadline": (
                        f"{item.hours_until_deadline:.1f}"
                        if item.hours_until_deadline is not None
                        else ""
                    ),
                    "set_aside_code": item.set_aside_code,
                    "set_aside_description": item.set_aside_description,
                    "psc": item.psc,
                    "reasons": join_text(item.reasons),
                    "warnings": join_text(item.warnings),
                    "rejection_reasons": join_text(item.rejection_reasons),
                    "link": item.link,
                }
            )


def paragraph_safe(value: str) -> str:
    return escape(value or "")


def write_pdf(
    included: Sequence[Evaluation],
    all_evaluations: Sequence[Evaluation],
) -> None:
    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=letter,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
    )
    styles = getSampleStyleSheet()
    story: list[Any] = []

    bucket_counts = Counter(item.bucket for item in all_evaluations)
    story.append(Paragraph("SAM.gov Low-Hanging-Fruit RFQs", styles["Title"]))
    story.append(
        Paragraph(
            paragraph_safe(
                f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | "
                f"Strong: {bucket_counts['strong']} | "
                f"Conditional: {bucket_counts['conditional']} | "
                f"Rejected: {bucket_counts['rejected']}"
            ),
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 0.18 * inch))

    if not included:
        story.append(
            Paragraph(
                "No new or materially updated opportunities met the configured "
                "thresholds. Review the attached audit CSV for all fetched records "
                "and exact rejection reasons.",
                styles["Normal"],
            )
        )
    else:
        for bucket, heading in (
            ("strong", "Strong RFQs"),
            ("conditional", "Conditional RFQs"),
        ):
            items = [item for item in included if item.bucket == bucket]
            if not items:
                continue

            story.append(Paragraph(heading, styles["Heading1"]))
            for item in items:
                title = (
                    f"[{item.change_status}] {item.title}"
                    f" — Score {item.score}"
                )
                story.append(Paragraph(paragraph_safe(title), styles["Heading3"]))

                identity = (
                    f"<b>Solicitation:</b> {paragraph_safe(item.solicitation_number or 'Not listed')} "
                    f"&nbsp;|&nbsp; <b>PSC:</b> {paragraph_safe(item.psc or 'Not listed')} "
                    f"&nbsp;|&nbsp; <b>Set-aside:</b> "
                    f"{paragraph_safe(item.set_aside_description or item.set_aside_code or 'Unclear')}"
                )
                story.append(Paragraph(identity, styles["Normal"]))

                timing = (
                    f"<b>Posted:</b> {paragraph_safe(item.posted_date or 'Not listed')} "
                    f"&nbsp;|&nbsp; <b>Due:</b> "
                    f"{paragraph_safe(item.deadline_text or 'Not listed')}"
                )
                story.append(Paragraph(timing, styles["Normal"]))
                story.append(
                    Paragraph(
                        f"<b>Agency:</b> {paragraph_safe(item.agency)}",
                        styles["Normal"],
                    )
                )

                if item.reasons:
                    story.append(
                        Paragraph(
                            f"<b>Why it surfaced:</b> "
                            f"{paragraph_safe('; '.join(item.reasons))}",
                            styles["Normal"],
                        )
                    )
                if item.warnings:
                    story.append(
                        Paragraph(
                            f"<b>Watch:</b> "
                            f"{paragraph_safe('; '.join(item.warnings))}",
                            styles["Normal"],
                        )
                    )
                if item.link:
                    safe_link = paragraph_safe(item.link)
                    story.append(
                        Paragraph(
                            f'<link href="{safe_link}">{safe_link}</link>',
                            styles["Normal"],
                        )
                    )

                story.append(Spacer(1, 0.08 * inch))
                story.append(HRFlowable(width="100%", color="#cccccc"))
                story.append(Spacer(1, 0.12 * inch))

    story.append(Spacer(1, 0.12 * inch))
    story.append(
        Paragraph(
            "The CSV audit contains every fetched notice, including rejected "
            "items and the exact filter reason. Use it to adjust thresholds "
            "without guessing what disappeared.",
            styles["Italic"],
        )
    )
    doc.build(story)


def build_email_body(
    included: Sequence[Evaluation],
    all_evaluations: Sequence[Evaluation],
) -> str:
    counts = Counter(item.bucket for item in included)
    fetched_counts = Counter(item.bucket for item in all_evaluations)
    return (
        "SAM.gov low-hanging-fruit scan complete.\n\n"
        f"New/materially updated items attached:\n"
        f"- Strong RFQs: {counts['strong']}\n"
        f"- Conditional RFQs: {counts['conditional']}\n\n"
        f"All fetched RFQ-stage notices evaluated:\n"
        f"- Strong: {fetched_counts['strong']}\n"
        f"- Conditional: {fetched_counts['conditional']}\n"
        f"- Rejected: {fetched_counts['rejected']}\n\n"
        "The PDF contains the surfaced opportunities. The CSV contains every "
        "record and its score, warnings, and exact rejection reason."
    )


def attach_file(message: MIMEMultipart, path: Path, mime_subtype: str) -> None:
    with path.open("rb") as handle:
        part = MIMEBase("application", mime_subtype)
        part.set_payload(handle.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{path.name}"',
    )
    message.attach(part)


def email_is_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASS and EMAIL_TO)


def send_email(
    included: Sequence[Evaluation],
    all_evaluations: Sequence[Evaluation],
) -> bool:
    if not email_is_configured():
        print(
            "Email is not configured. Files were generated locally but no email "
            "was sent. Set SMTP_USER, SMTP_PASS, and EMAIL_TO.",
            flush=True,
        )
        return False

    counts = Counter(item.bucket for item in included)
    subject = (
        f"SAM.gov opportunities — "
        f"{counts['strong']} strong, "
        f"{counts['conditional']} conditional — "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    )

    message = MIMEMultipart()
    message["From"] = SMTP_USER
    message["To"] = EMAIL_TO
    message["Subject"] = subject
    message.attach(
        MIMEText(
            build_email_body(included, all_evaluations),
            "plain",
            "utf-8",
        )
    )

    attach_file(message, PDF_PATH, "pdf")
    attach_file(message, AUDIT_CSV_PATH, "octet-stream")

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=60) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(message)

    print(f"Email sent to {EMAIL_TO}.", flush=True)
    return True


def print_console_summary(
    evaluations: Sequence[Evaluation],
    print_top: int,
) -> None:
    counts = Counter(item.bucket for item in evaluations)
    print(
        "\nEvaluation summary: "
        f"{counts['strong']} strong RFQs, "
        f"{counts['conditional']} conditional RFQs, "
        f"{counts['rejected']} rejected.",
        flush=True,
    )

    actionable = [item for item in evaluations if item.is_actionable]
    for item in actionable[:print_top]:
        print(
            f"[{item.bucket.upper():11}] score={item.score:>2} "
            f"{item.solicitation_number or item.notice_id} — {item.title}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan SAM.gov for low-hanging-fruit supply opportunities."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the PDF/CSV but do not email or update seen-state.",
    )
    parser.add_argument(
        "--reset-seen",
        action="store_true",
        help="Delete the v2 seen-state file before scanning.",
    )
    parser.add_argument(
        "--print-top",
        type=int,
        default=15,
        help="Number of surfaced opportunities to print to the console.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not SAM_API_KEY:
        print(
            "SAM_API_KEY is not set. Add it as an environment variable before running.",
            file=sys.stderr,
        )
        return 2

    if args.reset_seen and STATE_FILE.exists():
        STATE_FILE.unlink()
        print(f"Deleted {STATE_FILE}.", flush=True)

    state = load_state()

    try:
        opportunities = fetch_recent_opportunities()
    except Exception as exc:
        print(f"Scan failed: {exc}", file=sys.stderr)
        return 1

    evaluations = evaluate_all(opportunities, state)
    print_console_summary(evaluations, max(args.print_top, 0))
    write_audit_csv(evaluations)

    surfaced = [
        item
        for item in evaluations
        if item.is_actionable
        and item.change_status in {"NEW", "UPDATED"}
    ][:MAX_EMAIL_ITEMS]

    # Legacy-seen records are not re-emailed merely because the storage format
    # changed. Delete the v2 state file or use --reset-seen for a clean rescan.
    write_pdf(surfaced, evaluations)

    print(f"\nPDF:   {PDF_PATH}", flush=True)
    print(f"Audit: {AUDIT_CSV_PATH}", flush=True)

    if args.dry_run:
        print("Dry run: no email sent and seen-state not changed.", flush=True)
        return 0

    if surfaced:
        sent = send_email(surfaced, evaluations)
        if sent or MARK_SEEN_WITHOUT_EMAIL:
            mark_emailed(state, surfaced)
            save_state(state)
    elif SEND_ZERO_RESULT_EMAIL:
        sent = send_email([], evaluations)
        if sent:
            save_state(state)
    else:
        print("No new or materially updated opportunities to email.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
