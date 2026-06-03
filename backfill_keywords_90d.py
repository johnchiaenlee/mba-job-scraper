#!/usr/bin/env python3
"""
One-time 90-day keyword backfill.

Runs all 44 keywords with a 90-day window (instead of the daily 25-hour window)
to surface MBA jobs posted in the past 3 months that were missed.
Same filtering logic as job_scraper.py (Layer 1 + LLM + dedup).

Run once via GitHub Actions, then delete this file.
"""

import json
import os
import re
import time
from datetime import datetime

import anthropic
import gspread
import pandas as pd
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from jobspy import scrape_jobs

# ── Config (90-day override) ──────────────────────────────────────────────────

SHEET_ID   = "1M5SaGYmAFZAbtxCYwDRz68jXddnBlcvuZNbhnSKFIg8"
SHEET_TAB  = "Sheet1"

KEYWORDS_GENERIC = [
    "MBA Strategy Intern",
    "MBA Corporate Strategy Intern",
    "MBA Strategic Planning Intern",
    "MBA Operations Intern",
    "MBA Business Operations Intern",
    "MBA Operations Summer Associate",
    "MBA Supply Chain Intern",
    "MBA Supply Chain Summer Intern",
    "MBA Logistics Intern",
    "MBA Program Manager Intern",
    "MBA Program Management Intern",
    "MBA Project Management Intern",
    "MBA Summer Associate",
    "MBA Rotational Program",
    "MBA Business Development Intern",
    "MBA Summer Intern",
]

KEYWORDS_COMPANY = [
    "Amazon MBA intern",
    "Google MBA intern",
    "Microsoft MBA intern",
    "Apple MBA intern",
    "Meta MBA intern",
    "Salesforce MBA intern",
    "Adobe MBA intern",
    "ServiceNow MBA intern",
    "Oracle MBA intern",
    "Capital One MBA intern",
    "Visa MBA intern",
    "American Express MBA intern",
    "Pfizer MBA intern",
    "Genentech MBA intern",
    "Amgen MBA intern",
    "Gilead MBA intern",
    "Kaiser Permanente MBA intern",
    "UnitedHealth MBA intern",
    "Mattel MBA intern",
    "General Mills MBA intern",
    "PepsiCo MBA intern",
    "Starbucks MBA intern",
    "Walmart MBA intern",
    "Warner Bros MBA intern",
    "NBCUniversal MBA intern",
    "Paramount MBA intern",
    "Live Nation MBA intern",
]

KEYWORDS = KEYWORDS_GENERIC + KEYWORDS_COMPANY   # 43 total

RESULTS_PER_KEYWORD = 100   # wider net for 90-day window
HOURS_OLD           = 2160  # 90 days
SCRAPE_SLEEP_SEC    = 5
LLM_SLEEP_SEC       = 0.3

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

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
    "phd", "ph.d",
]

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
        specifying a non-MBA degree.
    Set is_mba_targeted=false for:
    - Any permanent full-time role
    - Internships aimed at undergrads only
    - Roles targeting PhD / doctoral candidates or academic researchers
    - Roles targeting MS students in specific non-business fields
    - Research assistant/fellow at think-tanks, universities, non-profits
    - Administrative Fellowships at healthcare systems, hospitals, universities,
      or government orgs (target MHA/MPH graduates, NOT MBAs)
    - Roles where "MBA" appears only incidentally (e.g. in company name)
    When in doubt, default to false.
"""

# ── Helpers (same as job_scraper.py) ─────────────────────────────────────────

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
    try:
        formulas = ws.get("B2:B5000", value_render_option="FORMULA")
    except Exception as e:
        print(f"[warn] Could not read existing URLs: {e}")
        return set()
    urls = set()
    for row in formulas:
        if row:
            m = re.search(r'HYPERLINK\("([^"]+)"', str(row[0]))
            if m:
                urls.add(m.group(1))
    return urls


def passes_title_prefilter(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TITLE_INTERN_SIGNALS) and \
           not any(kw in t for kw in TITLE_HARD_EXCLUDES)


def safe(val) -> str:
    s = str(val) if val is not None else ""
    return "" if s.lower() in ("nan", "none", "<na>") else s


def build_location(job: pd.Series) -> str:
    city  = safe(job.get("city", ""))
    state = safe(job.get("state", ""))
    if city or state:
        return ", ".join(p for p in [city, state] if p)
    loc = safe(job.get("location", ""))
    return loc if loc else "United States"


def format_posted_date(job: pd.Series) -> str:
    raw = job.get("date_posted")
    if raw is None or pd.isna(raw):
        return ""
    try:
        if hasattr(raw, "date"):
            posted = raw.date()
        elif isinstance(raw, str):
            posted = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        else:
            posted = raw
        return posted.strftime("%-m/%-d/%Y")
    except Exception:
        return ""


def fetch_date_from_page(url: str) -> str:
    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") == "JobPosting"), {})
                if data.get("@type") == "JobPosting" and data.get("datePosted"):
                    return datetime.strptime(data["datePosted"][:10], "%Y-%m-%d").strftime("%-m/%-d/%Y")
            except Exception:
                continue
        t = soup.find("time")
        if t and t.get("datetime"):
            return datetime.strptime(t["datetime"][:10], "%Y-%m-%d").strftime("%-m/%-d/%Y")
    except Exception:
        pass
    return ""


def classify_job(client: anthropic.Anthropic, title: str, company: str, description: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(
            title=title,
            company=company,
            description=(description or "")[:6000],
        )}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


def hyperlink(url: str, title: str) -> str:
    safe_title = title.replace('"', "'").replace("\n", " ")
    return f'=HYPERLINK("{url}","{safe_title}")'


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n=== 90-Day Keyword Backfill  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===")
    print(f"Keywords: {len(KEYWORDS)}  |  Results/keyword: {RESULTS_PER_KEYWORD}  |  Window: 90 days\n")

    # 1. Load existing URLs for dedup
    print("Connecting to Google Sheets...")
    ws = get_worksheet()
    existing_urls = get_existing_urls(ws)
    print(f"Existing jobs in sheet: {len(existing_urls)}\n")

    # 2. Scrape all keywords with 90-day window
    print("Scraping LinkedIn (90-day window)...")
    frames = []
    for kw in KEYWORDS:
        print(f"  '{kw}' ...")
        try:
            df = scrape_jobs(
                site_name=["linkedin"],
                search_term=kw,
                location="United States",
                results_wanted=RESULTS_PER_KEYWORD,
                hours_old=HOURS_OLD,
                linkedin_fetch_description=True,
            )
            print(f"    → {len(df)} results")
            frames.append(df)
        except Exception as e:
            print(f"    → Error: {e}")
        time.sleep(SCRAPE_SLEEP_SEC)

    if not frames:
        print("No results scraped. Exiting.")
        return

    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["job_url"])
    combined["_dedup_key"] = (
        combined["title"].str.lower().str.strip() + "|" +
        combined["company"].str.lower().str.strip()
    )
    combined = combined.drop_duplicates(subset=["_dedup_key"]).drop(columns=["_dedup_key"])
    print(f"\nTotal unique scraped: {len(combined)}")

    # 3. Filter out already-existing jobs
    new_jobs = combined[~combined["job_url"].isin(existing_urls)].copy()
    print(f"New (after dedup against sheet): {len(new_jobs)}\n")
    if new_jobs.empty:
        print("No new jobs to add. Exiting.")
        return

    # 4. Filter and classify
    client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    rows    = []
    skipped = 0

    for _, job in new_jobs.iterrows():
        title   = safe(job.get("title"))
        company = safe(job.get("company"))
        desc    = safe(job.get("description"))
        url     = safe(job.get("job_url"))

        if not passes_title_prefilter(title):
            print(f"  [L1 skip] {title}")
            skipped += 1
            continue

        print(f"  Classifying: {title} @ {company}")
        try:
            result = classify_job(client, title, company, desc)
        except Exception as e:
            print(f"    [error] {e} — skipping")
            skipped += 1
            continue

        if not result.get("is_mba_targeted", True):
            print(f"    → [L2 skip] not MBA-targeted")
            skipped += 1
            continue

        posted = format_posted_date(job)
        if not posted:
            print(f"    → date_posted missing, fetching from page...")
            posted = fetch_date_from_page(url)
            print(f"    → {'fetched: ' + posted if posted else 'no date found'}")
            time.sleep(2)

        rows.append([
            company,
            hyperlink(url, title),
            ", ".join(result.get("function", [])),
            build_location(job),
            "Open",
            result.get("sponsorship", "Not Specified"),
            result.get("deadline") or "",
            posted,
        ])

        time.sleep(LLM_SLEEP_SEC)

    # 5. Write to sheet
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"\n✓ Added {len(rows)} new jobs to '{SHEET_TAB}'.")
    else:
        print("\nNo qualifying MBA jobs found to add.")

    print(f"  Skipped: {skipped} (pre-filter + LLM filter + errors)")
    print("\nDone.\n")


if __name__ == "__main__":
    main()
