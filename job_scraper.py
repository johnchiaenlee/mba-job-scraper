#!/usr/bin/env python3
"""
MBA LinkedIn Job Scraper
========================
Runs daily via GitHub Actions at ~5am LA time.
  1. Scrapes LinkedIn using JobSpy across 16 targeted keywords
  2. Filters results with a 2-layer quality check:
       Layer 1 — title pre-filter  (free, instant)
       Layer 2 — Claude Haiku LLM  (paid, thorough)
  3. Deduplicates against existing Google Sheet rows
  4. Appends new qualifying jobs to the sheet

Column layout (Google Sheet):
  A = Company
  B = Role Title (=HYPERLINK formula, clickable)
  C = Function  (Strategy / Ops / PGM)
  D = Location
  E = Status    (Open / Closed / Not MBA)
  F = Sponsorship
  G = Application Deadline
  H = Posted    (actual date M/D/YYYY — col I formula computes age)

Required environment variables:
  ANTHROPIC_API_KEY        — Anthropic API key
  GOOGLE_CREDENTIALS_JSON  — Full JSON of a Google service-account key (as string)
"""

import json
import os
import re
import time
from datetime import datetime

import anthropic
import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from jobspy import scrape_jobs

# ── Configuration ─────────────────────────────────────────────────────────────

SHEET_ID   = "1M5SaGYmAFZAbtxCYwDRz68jXddnBlcvuZNbhnSKFIg8"
SHEET_TAB  = "Sheet1"

# Keywords = generic function keywords + company-specific keywords
# Generic: surface the long tail across all companies
# Company-specific: ensure top alumni employers are always checked
# LLM handles all quality filtering — broad input is intentional.

KEYWORDS_GENERIC = [
    # ── Strategy ──────────────────────────────────────────────────────────────
    "MBA Strategy Intern",
    "MBA Corporate Strategy Intern",
    "MBA Strategic Planning Intern",
    # ── Operations ────────────────────────────────────────────────────────────
    "MBA Operations Intern",
    "MBA Business Operations Intern",
    "MBA Operations Summer Associate",
    # ── Supply Chain ──────────────────────────────────────────────────────────
    "MBA Supply Chain Intern",
    "MBA Supply Chain Summer Intern",
    "MBA Logistics Intern",
    # ── Program / Project Management ──────────────────────────────────────────
    "MBA Program Manager Intern",
    "MBA Program Management Intern",
    "MBA Project Management Intern",
    # ── General / Cross-functional ────────────────────────────────────────────
    "MBA Summer Associate",
    "MBA Rotational Program",
    "MBA Business Development Intern",
    "MBA Summer Intern",
]

KEYWORDS_COMPANY = [
    # ── Tech ──────────────────────────────────────────────────────────────────
    "Amazon MBA intern",
    "Google MBA intern",
    "Microsoft MBA intern",
    "Apple MBA intern",
    "Meta MBA intern",
    "Salesforce MBA intern",
    "Adobe MBA intern",
    "ServiceNow MBA intern",
    "Oracle MBA intern",
    # ── Financial Services ────────────────────────────────────────────────────
    "Capital One MBA intern",
    "Visa MBA intern",
    "American Express MBA intern",
    # ── Healthcare / Pharma ───────────────────────────────────────────────────
    "Pfizer MBA intern",
    "Genentech MBA intern",
    "Amgen MBA intern",
    "Gilead MBA intern",
    "Kaiser Permanente MBA intern",
    "UnitedHealth MBA intern",
    # ── Consumer / Retail / CPG ───────────────────────────────────────────────
    "Mattel MBA intern",
    "General Mills MBA intern",
    "PepsiCo MBA intern",
    "Starbucks MBA intern",
    "Walmart MBA intern",
    # ── Media / Entertainment ─────────────────────────────────────────────────
    "Warner Bros MBA intern",
    "NBCUniversal MBA intern",
    "Paramount MBA intern",
    "Live Nation MBA intern",
]

KEYWORDS = KEYWORDS_GENERIC + KEYWORDS_COMPANY

RESULTS_PER_KEYWORD = 20         # ~60 keywords × 20 = ~1200 raw before dedup
HOURS_OLD           = 25         # slightly > 24 h to cover timezone edge cases
SCRAPE_SLEEP_SEC    = 5          # pause between keywords to avoid LinkedIn rate-limit
LLM_SLEEP_SEC       = 0.3        # pause between Haiku calls

# ── Layer 1: Title pre-filter ──────────────────────────────────────────────────
# Fast keyword check — zero API cost.
# Rejects obviously wrong titles before spending money on LLM classification.
#
# PASS rules  (must match at least one):
#   intern, internship, co-op, coop, mba, fellowship,
#   summer associate, rotational, associate program
#
# FAIL rules  (auto-reject even if a pass signal exists):
#   senior, director, vp, principal, head of, staff, chief,
#   phd, ph.d  ← PhD roles should never reach LLM
#
# Edge cases left to Layer 2 (LLM):
#   - Administrative Fellowship (MHA track, not MBA)
#   - Research assistant / research fellow (PhD track)
#   - MS-targeted roles (non-business master's)
#   - Generic "Intern" with no MBA / graduate signal

TITLE_INTERN_SIGNALS = [
    "intern", "internship", "co-op", "coop",
    "mba", "fellowship", "summer associate",
    "rotational", "associate program",
]

TITLE_HARD_EXCLUDES = [
    "senior ", "sr.", "sr ", " sr ",
    "director", "vp ", "vice president",
    "principal", "head of", "staff ",
    "chief ", "president", "c-suite",
    "phd", "ph.d",              # doctoral roles — never MBA
]


def passes_title_prefilter(title: str) -> bool:
    """Return True only if title looks like an internship/MBA-level role."""
    t = title.lower()
    has_intern_signal  = any(kw in t for kw in TITLE_INTERN_SIGNALS)
    has_senior_signal  = any(kw in t for kw in TITLE_HARD_EXCLUDES)
    return has_intern_signal and not has_senior_signal


# ── Google Sheets helpers ──────────────────────────────────────────────────────

def get_worksheet() -> gspread.Worksheet:
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"]),
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)


def get_existing_urls(ws: gspread.Worksheet) -> set:
    """
    Column B stores =HYPERLINK("url","title") formulas.
    Read raw formulas and regex-extract the URL for dedup.
    Returns a set of LinkedIn job URLs already in the sheet.
    """
    try:
        formulas = ws.get("B2:B5000", value_render_option="FORMULA")
    except Exception as e:
        print(f"[warn] Could not read existing URLs from sheet: {e}")
        return set()

    urls = set()
    for row in formulas:
        if row:
            m = re.search(r'HYPERLINK\("([^"]+)"', str(row[0]))
            if m:
                urls.add(m.group(1))
    return urls


# ── Scraping ───────────────────────────────────────────────────────────────────

def scrape_all_keywords() -> pd.DataFrame:
    """Scrape LinkedIn for every keyword, combine, and deduplicate."""
    frames = []
    for kw in KEYWORDS:
        print(f"  Scraping: '{kw}' ...")
        try:
            df = scrape_jobs(
                site_name=["linkedin"],
                search_term=kw,
                location="United States",
                results_wanted=RESULTS_PER_KEYWORD,
                hours_old=HOURS_OLD,
                linkedin_fetch_description=True,  # required for LLM classification
            )
            print(f"    → {len(df)} results")
            frames.append(df)
        except Exception as e:
            print(f"    → Error scraping '{kw}': {e}")

        time.sleep(SCRAPE_SLEEP_SEC)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    # Primary dedup: same job URL
    combined = combined.drop_duplicates(subset=["job_url"])

    # Secondary dedup: same title+company catches re-posts with different URLs
    combined["_dedup_key"] = (
        combined["title"].str.lower().str.strip() + "|" +
        combined["company"].str.lower().str.strip()
    )
    combined = combined.drop_duplicates(subset=["_dedup_key"]).drop(columns=["_dedup_key"])
    return combined


# ── Layer 2: LLM Classification ───────────────────────────────────────────────
#
# Target audience (is_mba_targeted = true):
#   ✅ MBA students / MBA candidates explicitly mentioned
#   ✅ Generic "graduate intern" / "graduate program" (degree unspecified)
#
# Always reject (is_mba_targeted = false):
#   ❌ Permanent full-time roles
#   ❌ Undergrad-only internships
#   ❌ PhD / doctoral candidates or academic researchers
#   ❌ MS in non-business fields (Engineering, CS, Public Policy, Data Science…)
#   ❌ Research assistant / fellow at think-tanks, universities, non-profits
#   ❌ Administrative Fellowships at healthcare / university / government orgs
#      (these target MHA/MPH graduates, NOT MBAs)
#   ❌ "MBA" appearing only incidentally (company name, unrelated context)
#
# When in doubt → false

CLASSIFY_PROMPT = """\
Analyze this LinkedIn job posting and return a JSON classification.

Title: {title}
Company: {company}
Description:
{description}

Return ONLY a valid JSON object — no markdown fences, no explanation:
{{
  "function": [],
  "sponsorship": "Not Specified",
  "deadline": null,
  "is_mba_targeted": true
}}

Strict rules:
- function: list of applicable tags from ["Strategy", "Ops", "PGM"] only.
    Strategy = corporate/business strategy, strategic finance, strategy & analytics.
    Ops      = operations, supply chain, process improvement, logistics.
    PGM      = program management, project management, cross-functional coordination.
    Must include at least one tag. Can include multiple.

- sponsorship: "Yes" if posting explicitly offers visa sponsorship.
               "No" if posting explicitly states no sponsorship is available.
               "Not Specified" if not mentioned.

- deadline: Search the description carefully for any application deadline. Look for
    phrases like "apply by", "applications close", "application deadline",
    "position closes", "submit by", "priority deadline", or specific dates near
    "deadline" / "close" / "due". Format as "M/D/YYYY". Return null ONLY if truly absent.

- is_mba_targeted: true ONLY if ALL of the following hold:
    (a) The role is a temporary/internship position (summer intern, co-op, fellowship,
        rotational program, or similar) — NOT a permanent full-time hire.
    (b) The role targets MBA students OR uses generic "graduate" language without
        specifying a non-MBA degree. Acceptable: "MBA", "MBA student", "MBA candidate",
        "graduate intern", "graduate program" (degree unspecified).
    Set is_mba_targeted=false for:
    - Any permanent full-time role (Manager, Consultant, Analyst, Engineer, etc.)
    - Internships aimed at undergrads only
    - Roles targeting PhD / doctoral candidates or academic researchers
    - Roles targeting MS students in specific non-business fields
      (e.g. MS Engineering, MS Computer Science, MS Public Policy, MS Data Science)
    - Roles where primary work is academic research, literature review, or policy
      analysis with no business component (research assistant/fellow at think-tanks,
      universities, non-profits)
    - Administrative Fellowships at healthcare systems, hospitals, universities,
      or government orgs — these target MHA/MPH/public admin graduates, NOT MBAs
      (e.g. "Administrative Fellowship", "Health Plan Operations Fellowship")
    - Roles where "MBA" appears only incidentally (e.g. in company name)
    When in doubt, default to false.
"""


def classify_job(client: anthropic.Anthropic, title: str, company: str, description: str) -> dict:
    """Call Claude Haiku to classify a single job. Returns parsed JSON dict."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": CLASSIFY_PROMPT.format(
                title=title,
                company=company,
                description=(description or "")[:6000],  # cap to avoid token overflow
            ),
        }],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ── Row-building helpers ───────────────────────────────────────────────────────

def safe(val) -> str:
    """Convert a pandas value to a clean string; treat NaN/None/<NA> as empty."""
    s = str(val) if val is not None else ""
    return "" if s.lower() in ("nan", "none", "<na>") else s


def build_location(job: pd.Series) -> str:
    """
    Build a human-readable location string.
    Priority: city+state → full location string → "United States"
    JobSpy's city/state fields are often empty for LinkedIn; the location
    field (e.g. "New York, NY") is the most reliable fallback.
    """
    city  = safe(job.get("city", ""))
    state = safe(job.get("state", ""))
    if city or state:
        return ", ".join(p for p in [city, state] if p)
    loc = safe(job.get("location", ""))
    return loc if loc else "United States"


def format_posted_date(job: pd.Series) -> str:
    """
    Return the actual post date as M/D/YYYY for Google Sheets.
    Column I uses a formula to auto-compute relative age (e.g. "3 days ago").

    Handles all pandas null variants (None, NaN float, NaT) — returns ""
    rather than "nan" / "NaT" which would break the Sheets formula.
    """
    raw = job.get("date_posted")
    if raw is None or pd.isna(raw):   # catches None, float NaN, pd.NaT
        return ""
    try:
        if hasattr(raw, "date"):
            posted = raw.date()       # datetime / Timestamp → date
        elif isinstance(raw, str):
            posted = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        else:
            posted = raw
        return posted.strftime("%-m/%-d/%Y")
    except Exception:
        return ""                     # never write "NaT" or garbage to the sheet


def hyperlink(url: str, title: str) -> str:
    """Return a =HYPERLINK() formula safe for Google Sheets USER_ENTERED input."""
    safe_title = title.replace('"', "'").replace("\n", " ")
    return f'=HYPERLINK("{url}","{safe_title}")'


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n=== MBA Job Scraper  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===\n")

    # 1. Connect to Google Sheets and load existing job URLs
    print("Connecting to Google Sheets...")
    ws = get_worksheet()
    existing_urls = get_existing_urls(ws)
    print(f"Existing jobs in sheet: {len(existing_urls)}\n")

    # 2. Scrape LinkedIn
    print("Scraping LinkedIn...")
    jobs = scrape_all_keywords()
    if jobs.empty:
        print("\nNo jobs scraped. Exiting.")
        return

    # 3. Deduplicate against existing sheet
    new_jobs = jobs[~jobs["job_url"].isin(existing_urls)].copy()
    print(f"\nRaw scraped: {len(jobs)}  |  New (after dedup): {len(new_jobs)}\n")
    if new_jobs.empty:
        print("Sheet is already up to date. Exiting.")
        return

    # 4. Filter and classify each new job
    client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    rows    = []
    skipped = 0

    for _, job in new_jobs.iterrows():
        title   = safe(job.get("title"))
        company = safe(job.get("company"))
        desc    = safe(job.get("description"))
        url     = safe(job.get("job_url"))

        # ── Layer 1: title pre-filter (free) ──────────────────────────────────
        if not passes_title_prefilter(title):
            print(f"    → [L1 skip] '{title}'")
            skipped += 1
            continue

        # ── Layer 2: LLM classification ───────────────────────────────────────
        print(f"  Classifying: {title} @ {company}")
        try:
            result = classify_job(client, title, company, desc)
        except Exception as e:
            print(f"    [error] LLM failed ({e}) — skipping")
            skipped += 1
            continue

        if not result.get("is_mba_targeted", True):
            print(f"    → [L2 skip] not MBA-targeted")
            skipped += 1
            continue

        # ── Build row ─────────────────────────────────────────────────────────
        rows.append([
            company,
            hyperlink(url, title),
            ", ".join(result.get("function", [])),
            build_location(job),
            "Open",
            result.get("sponsorship", "Not Specified"),
            result.get("deadline") or "",
            format_posted_date(job),
        ])

        time.sleep(LLM_SLEEP_SEC)

    # 5. Append qualifying rows to Google Sheet
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"\n✓ Added {len(rows)} new jobs to '{SHEET_TAB}'.")
    else:
        print("\nNo qualifying MBA jobs found to add.")

    print(f"  Skipped: {skipped} (title filter + LLM filter + errors)")
    print("\nDone.\n")


if __name__ == "__main__":
    main()
