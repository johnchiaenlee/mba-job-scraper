#!/usr/bin/env python3
"""
MBA LinkedIn Job Scraper
========================
Runs daily via GitHub Actions at ~5am LA time.
  1. Scrapes LinkedIn using JobSpy
  2. Deduplicates against existing Google Sheet rows
  3. Classifies each new job with Claude Haiku
  4. Appends qualifying jobs to the sheet

Required environment variables:
  ANTHROPIC_API_KEY        — Anthropic API key
  GOOGLE_CREDENTIALS_JSON  — Full JSON of a Google service-account key (as a string)
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
SHEET_TAB  = "Sheet1"            # must match your tab name exactly

KEYWORDS   = [
    # ── Strategy ──────────────────────────────────────────────────────────────
    "MBA Strategy Intern",              # broad — catches F500 & startups alike
    "MBA Corporate Strategy Intern",    # large-co titles (Google, Amazon, etc.)
    "MBA Strategic Planning Intern",    # mid-size ops-heavy companies
    # ── Operations ────────────────────────────────────────────────────────────
    "MBA Operations Intern",
    "MBA Business Operations Intern",   # tech/growth-stage companies
    "MBA Operations Summer Associate",  # consulting & finance orgs
    # ── Supply Chain ──────────────────────────────────────────────────────────
    "MBA Supply Chain Intern",
    "MBA Supply Chain Summer Intern",
    "MBA Logistics Intern",             # 3PL, retail, manufacturing
    # ── Program / Project Management ──────────────────────────────────────────
    "MBA Program Manager Intern",
    "MBA Program Management Intern",
    "MBA Project Management Intern",    # tech companies (FAANG, etc.)
    # ── General / Cross-functional ────────────────────────────────────────────
    "MBA Summer Associate",             # finance & consulting catch-all
    "MBA Rotational Program",           # LDP / rotational at large cos
    "MBA Business Development Intern",  # growth / BD roles
    "MBA Summer Intern",                # small & mid-size companies
]

RESULTS_PER_KEYWORD = 30         # 17 keywords × 30 = ~510 raw before dedup
HOURS_OLD           = 25         # slightly > 24 h to cover timezone edge cases
SCRAPE_SLEEP_SEC    = 6          # longer pause with more keywords to avoid rate-limit
LLM_SLEEP_SEC       = 0.3        # pause between LLM calls

VALID_FUNCTIONS = ["Strategy", "Ops", "PGM"]

# ── Title pre-filter ───────────────────────────────────────────────────────────
# A job title must contain at least one INTERN signal to pass.
# This catches full-time/senior roles before we spend any LLM calls on them.

TITLE_INTERN_SIGNALS = [
    "intern", "internship", "co-op", "coop",
    "mba", "fellowship", "summer associate",
    "rotational", "associate program",
]

# Titles containing these words are rejected even if an intern signal is present
# (e.g. "Senior MBA Program Manager" should not pass).
TITLE_HARD_EXCLUDES = [
    "senior ", "sr.", "sr ", " sr ",
    "director", "vp ", "vice president",
    "principal", "head of", "staff ",
    "chief ", "president", "c-suite",
]


def passes_title_prefilter(title: str) -> bool:
    """Return True only if title looks like an internship/MBA-level role."""
    t = title.lower()
    has_intern_signal = any(kw in t for kw in TITLE_INTERN_SIGNALS)
    has_senior_signal = any(kw in t for kw in TITLE_HARD_EXCLUDES)
    return has_intern_signal and not has_senior_signal

# ── Google Sheets helpers ─────────────────────────────────────────────────────

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
    We read raw formulas and regex-extract the URL for dedup.
    Returns a set of LinkedIn job URLs already in the sheet.
    """
    try:
        # value_render_option="FORMULA" returns the raw formula string
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

# ── Scraping ──────────────────────────────────────────────────────────────────

def scrape_all_keywords() -> pd.DataFrame:
    """Scrape LinkedIn for every keyword, combine, and deduplicate by job URL."""
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
                linkedin_fetch_description=True,  # needed for LLM classification
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
    # Secondary dedup: same title+company (catches re-posts with different URLs)
    combined["_dedup_key"] = (
        combined["title"].str.lower().str.strip() + "|" +
        combined["company"].str.lower().str.strip()
    )
    combined = combined.drop_duplicates(subset=["_dedup_key"]).drop(columns=["_dedup_key"])
    return combined

# ── LLM Classification ────────────────────────────────────────────────────────

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
    phrases like "apply by", "applications close", "application deadline", "position closes",
    "submit by", "priority deadline", "rolling admissions", or specific dates near
    "deadline" / "close" / "due". Format as "M/D/YYYY". Return null ONLY if truly absent.
- is_mba_targeted: true ONLY if ALL of the following hold:
    (a) The role is a temporary/internship position (summer intern, co-op, fellowship,
        rotational program, or similar) — NOT a permanent full-time hire.
    (b) The role explicitly targets MBA students, MBA candidates, or master's-level
        graduates (look for "MBA", "master's", "graduate program", "business school").
    Set is_mba_targeted=false for:
    - Any permanent full-time role (Manager, Consultant, Analyst, Engineer, etc.)
    - Internships aimed at undergrads only (no MBA/graduate mention)
    - Roles where "MBA" appears only incidentally (e.g. company name, unrelated context)
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
                description=(description or "")[:6000],  # cap tokens
            ),
        }],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if model adds them anyway
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)

# ── Row-building helpers ──────────────────────────────────────────────────────

def safe(val) -> str:
    """Convert a pandas value to a clean string; treat NaN as empty."""
    s = str(val) if val is not None else ""
    return "" if s.lower() in ("nan", "none", "<na>") else s


def build_location(job: pd.Series) -> str:
    # Prefer structured city + state fields
    city  = safe(job.get("city", ""))
    state = safe(job.get("state", ""))
    if city or state:
        parts = [p for p in [city, state] if p]
        return ", ".join(parts)
    # Fall back to JobSpy's full location string (e.g. "New York, NY")
    loc = safe(job.get("location", ""))
    return loc if loc else "United States"


def format_posted_date(job: pd.Series) -> str:
    """Return a human-readable relative string (e.g. '3 days ago') from date_posted."""
    raw = job.get("date_posted")
    if raw is None:
        return ""
    try:
        if hasattr(raw, "date"):          # datetime → date
            posted = raw.date()
        elif isinstance(raw, str):
            posted = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        else:
            posted = raw
        delta = (datetime.utcnow().date() - posted).days
        if delta == 0:
            return "Today"
        elif delta == 1:
            return "1 day ago"
        elif delta < 7:
            return f"{delta} days ago"
        elif delta < 14:
            return "1 week ago"
        elif delta < 30:
            return f"{delta // 7} weeks ago"
        elif delta < 60:
            return "1 month ago"
        else:
            return f"{delta // 30} months ago"
    except Exception:
        return str(raw)


def hyperlink(url: str, title: str) -> str:
    """Return a =HYPERLINK() formula safe for Google Sheets USER_ENTERED input."""
    safe_title = title.replace('"', "'").replace("\n", " ")
    return f'=HYPERLINK("{url}","{safe_title}")'

# ── Main ──────────────────────────────────────────────────────────────────────

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

    # 4. Classify each new job with Claude Haiku
    client    = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    rows      = []
    skipped   = 0

    for _, job in new_jobs.iterrows():
        title   = safe(job.get("title"))
        company = safe(job.get("company"))
        desc    = safe(job.get("description"))
        url     = safe(job.get("job_url"))

        # ── Layer 1: fast title pre-filter (no API cost) ──
        if not passes_title_prefilter(title):
            print(f"    → Skipped by title filter: '{title}'")
            skipped += 1
            continue

        print(f"  Classifying: {title} @ {company}")

        try:
            result = classify_job(client, title, company, desc)
        except Exception as e:
            print(f"    [error] LLM failed ({e}) — skipping")
            skipped += 1
            continue

        # ── Layer 2: LLM judgment ──
        if not result.get("is_mba_targeted", True):
            print(f"    → Skipped by LLM (not MBA-targeted)")
            skipped += 1
            continue

        functions  = ", ".join(result.get("function", []))
        sponsorship = result.get("sponsorship", "Not Specified")
        deadline    = result.get("deadline") or ""
        location    = build_location(job)

        # Column order: A=Company, B=Role/Link, C=Function, D=Location,
        #               E=Status, F=Sponsorship, G=Deadline, H=Posted
        rows.append([
            company,
            hyperlink(url, title),
            functions,
            location,
            "Open",
            sponsorship,
            deadline,
            format_posted_date(job),
        ])

        time.sleep(LLM_SLEEP_SEC)

    # 5. Append qualifying rows to Google Sheet
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"\n✓ Added {len(rows)} new jobs to '{SHEET_TAB}'.")
    else:
        print("\nNo qualifying MBA jobs found to add.")

    if skipped:
        print(f"  Skipped (LLM errors or not MBA-targeted): {skipped}")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
