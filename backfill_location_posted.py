#!/usr/bin/env python3
"""
One-time backfill: update existing sheet rows with proper Location and Posted date.

For every row that has a LinkedIn HYPERLINK in column B, this script:
  1. Re-scrapes LinkedIn (broad keywords, 90-day window) to find matching jobs by URL
  2. Updates column D (Location) with city/state instead of "United States"
  3. Writes column H (Posted) with relative posting date (e.g. "3 days ago")
  4. Ensures the H1 header says "Posted"

Run once via GitHub Actions, then delete this file.
"""

import json
import os
import re
import time
from datetime import datetime

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from jobspy import scrape_jobs

# ── Config ────────────────────────────────────────────────────────────────────

SHEET_ID  = "1M5SaGYmAFZAbtxCYwDRz68jXddnBlcvuZNbhnSKFIg8"
SHEET_TAB = "Sheet1"

KEYWORDS = [
    "MBA Strategy Internship",
    "MBA intern",
    "MBA internship",
    "summer associate MBA",
    "MBA rotational program",
    "MBA operations intern",
    "MBA program manager intern",
    "MBA supply chain intern",
    "MBA business development intern",
    "MBA finance intern",
]

RESULTS_PER_KEYWORD = 100
HOURS_OLD           = 2160   # 90 days — cast wide net to match old rows
SCRAPE_SLEEP_SEC    = 4

# ── Helpers ───────────────────────────────────────────────────────────────────

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


def safe(val) -> str:
    s = str(val) if val is not None else ""
    return "" if s.lower() in ("nan", "none", "<na>") else s


def build_location(job: pd.Series) -> str:
    city  = safe(job.get("city", ""))
    state = safe(job.get("state", ""))
    if city or state:
        parts = [p for p in [city, state] if p]
        return ", ".join(parts)
    loc = safe(job.get("location", ""))
    return loc if loc else "United States"


def format_posted_date(job: pd.Series) -> str:
    """Return actual post date as M/D/YYYY — Google Sheets formula computes age."""
    raw = job.get("date_posted")
    if raw is None:
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
        return str(raw)



# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n=== Backfill: Location + Posted  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===\n")

    # 1. Read existing sheet rows — extract URL → row-number map
    print("Reading Google Sheet...")
    ws = get_worksheet()

    formulas = ws.get("B1:B5000", value_render_option="FORMULA")
    url_to_row: dict[str, int] = {}
    for i, row in enumerate(formulas):
        if row:
            m = re.search(r'HYPERLINK\("([^"]+)"', str(row[0]))
            if m:
                url_to_row[m.group(1)] = i + 1  # 1-indexed sheet row

    print(f"Rows with LinkedIn URLs: {len(url_to_row)}")

    # Ensure H1 header is set
    ws.update("H1", [["Posted"]])

    # 2. Re-scrape LinkedIn broadly
    print("\nRe-scraping LinkedIn (90-day window)...")
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
                linkedin_fetch_description=False,  # no LLM needed here
            )
            print(f"    → {len(df)} results")
            frames.append(df)
        except Exception as e:
            print(f"    → Error: {e}")
        time.sleep(SCRAPE_SLEEP_SEC)

    if not frames:
        print("No results scraped. Exiting.")
        return

    scraped = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["job_url"])
    print(f"\nTotal unique scraped: {len(scraped)}")

    # 3. Match scraped jobs to existing rows and build batch update
    updates = []
    matched = 0

    for _, job in scraped.iterrows():
        url = safe(job.get("job_url", ""))
        if url not in url_to_row:
            continue

        row_num  = url_to_row[url]
        location = build_location(job)
        posted   = format_posted_date(job)

        updates.append({"range": f"D{row_num}", "values": [[location]]})
        updates.append({"range": f"H{row_num}", "values": [[posted]]})
        matched += 1
        print(f"  Row {row_num}: location='{location}'  posted='{posted}'")

    # 4. Write updates in one batch call
    if updates:
        ws.batch_update(updates)
        print(f"\n✓ Updated {matched} rows (location + posted date).")
    else:
        print("\nNo URL matches found — no rows updated.")
        print("This can happen if LinkedIn no longer serves those job URLs.")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
