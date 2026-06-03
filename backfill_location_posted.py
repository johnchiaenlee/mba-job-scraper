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
    if raw is None or pd.isna(raw):   # catches None, NaN, NaT
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
        return ""   # return empty, not "nan" / "NaT"



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
    updates  = []
    matched  = 0
    unmatched_urls = {}   # url → row_num, for rows still missing dates after keyword pass

    for _, job in scraped.iterrows():
        url = safe(job.get("job_url", ""))
        if url not in url_to_row:
            continue

        row_num  = url_to_row[url]
        location = build_location(job)
        posted   = format_posted_date(job)

        updates.append({"range": f"D{row_num}", "values": [[location]]})
        if posted:
            updates.append({"range": f"H{row_num}", "values": [[posted]]})
        else:
            unmatched_urls[url] = row_num   # has no date yet — try job-ID fetch next
        matched += 1
        print(f"  Row {row_num}: posted='{posted or '(no date from keyword search)'}'")

    # 3b. For rows still missing dates: fetch LinkedIn page directly and parse JSON-LD
    scraped_urls = {safe(j.get("job_url", "")) for _, j in scraped.iterrows()}
    still_empty  = {url: row for url, row in url_to_row.items()
                    if url not in scraped_urls}
    all_missing  = {**unmatched_urls, **still_empty}

    if all_missing:
        print(f"\nRound 3: fetching {len(all_missing)} pages directly for datePosted...")
        import requests
        from bs4 import BeautifulSoup

        HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        for url, row_num in all_missing.items():
            try:
                resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
                if resp.status_code != 200:
                    print(f"  Row {row_num}: HTTP {resp.status_code} — skipping")
                    time.sleep(2)
                    continue

                # Try JSON-LD first (most reliable)
                soup = BeautifulSoup(resp.text, "html.parser")
                date_str = None

                for tag in soup.find_all("script", type="application/ld+json"):
                    try:
                        data = json.loads(tag.string or "")
                        if isinstance(data, list):
                            data = next((d for d in data if d.get("@type") == "JobPosting"), {})
                        if data.get("@type") == "JobPosting" and data.get("datePosted"):
                            date_str = data["datePosted"][:10]   # "2026-05-13"
                            break
                    except Exception:
                        continue

                # Fallback: <time> tag
                if not date_str:
                    t = soup.find("time")
                    if t and t.get("datetime"):
                        date_str = t["datetime"][:10]

                if date_str:
                    from datetime import datetime as dt
                    posted = dt.strptime(date_str, "%Y-%m-%d").strftime("%-m/%-d/%Y")
                    updates.append({"range": f"H{row_num}", "values": [[posted]]})
                    print(f"  Row {row_num}: posted='{posted}' (via page scrape)")
                else:
                    print(f"  Row {row_num}: no date found in page")

            except Exception as e:
                print(f"  Row {row_num}: error — {e}")

            time.sleep(3)   # be polite, avoid rate-limit

    # 4. Write all updates in one batch call
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        print(f"\n✓ Batch update written.")
    else:
        print("\nNo updates to write.")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
