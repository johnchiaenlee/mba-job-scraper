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

SHEET_ID   = "19atxiLdiYTnGRGLwTFKRsqotBtGjxaVS"
SHEET_TAB  = "Job List"          # must match your tab name exactly

KEYWORDS   = [
    "MBA Strategy Internship",   # MVP: single keyword — add more here later
]

RESULTS_PER_KEYWORD = 20         # per keyword; LinkedIn caps ~200/search/IP
HOURS_OLD           = 25         # slightly > 24 h to cover timezone edge cases
SCRAPE_SLEEP_SEC    = 4          # pause between keyword scrapes (rate-limit safety)
LLM_SLEEP_SEC       = 0.3        # pause between LLM calls

VALID_FUNCTIONS = ["Strategy", "Ops", "PGM"]

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
    return combined.drop_duplicates(subset=["job_url"])

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
- deadline: application deadline formatted as "M/D/YYYY", or null if not stated.
- is_mba_targeted: true only if the role clearly targets MBA students, MBA interns,
    or recent MBA/master's graduates.
    false if it targets undergrad students or general applicants.
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
                description=(description or "")[:3000],  # cap tokens
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
    city  = safe(job.get("city", ""))
    state = safe(job.get("state", ""))
    parts = [p for p in [city, state] if p]
    return ", ".join(parts) if parts else "United States"


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

        print(f"  Classifying: {title} @ {company}")

        try:
            result = classify_job(client, title, company, desc)
        except Exception as e:
            print(f"    [error] LLM failed ({e}) — skipping")
            skipped += 1
            continue

        if not result.get("is_mba_targeted", True):
            print(f"    → Skipped (not MBA-targeted)")
            skipped += 1
            continue

        functions  = ", ".join(result.get("function", []))
        sponsorship = result.get("sponsorship", "Not Specified")
        deadline    = result.get("deadline") or ""
        location    = build_location(job)

        # Column order: A=Company, B=Role/Link, C=Function, D=Location,
        #               E=Status, F=Sponsorship, G=Deadline
        rows.append([
            company,
            hyperlink(url, title),
            functions,
            location,
            "Open",
            sponsorship,
            deadline,
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
