#!/usr/bin/env python3
"""
Weekly job-status checker.

For every row in the sheet where Status = "Open", ping the LinkedIn job URL.
If the job is no longer available, update Status → "Closed".

Signals that a job is closed:
  - HTTP 404
  - Response contains "No longer accepting applications"
  - Response contains "This job is no longer available"
  - Response contains "job has expired"
"""

import json
import os
import re
import time

import gspread
import requests
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

SHEET_ID       = "1M5SaGYmAFZAbtxCYwDRz68jXddnBlcvuZNbhnSKFIg8"
SHEET_TAB      = "Sheet1"
REQUEST_SLEEP  = 3      # seconds between URL checks (be polite to LinkedIn)
REQUEST_TIMEOUT = 10    # seconds

CLOSED_PHRASES = [
    # LinkedIn
    "no longer accepting applications",
    "this job is no longer available",
    "job has expired",
    "this position has been filled",
    "application deadline has passed",
    # MBA-Exchange: "We regret the job is no more available."
    "the job is no more available",
]

# LinkedIn redirects closed jobs to /jobs/search/?currentJobId=XXX
# instead of keeping them at /jobs/view/XXX — detect this redirect
LINKEDIN_CLOSED_REDIRECT = "/jobs/search/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

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


def is_closed(url: str) -> tuple[bool, str]:
    """
    Return (is_closed, reason) for a job URL.

    Detection signals:
      1. HTTP 404
      2. LinkedIn redirect: final URL contains /jobs/search/ (job removed)
         e.g. https://www.linkedin.com/jobs/search/?currentJobId=XXX&...
      3. Page body contains known "closed" phrases
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                            allow_redirects=True)

        # Signal 1: 404
        if resp.status_code == 404:
            return True, "HTTP 404"

        # Signal 2: LinkedIn redirected to search page (job removed/closed)
        if LINKEDIN_CLOSED_REDIRECT in resp.url:
            return True, f"redirected → {resp.url[:80]}"

        # Signal 3: Closed phrases in body
        body = resp.text.lower()
        for phrase in CLOSED_PHRASES:
            if phrase in body:
                return True, f'phrase: "{phrase}"'

        return False, ""
    except Exception as e:
        print(f"    [warn] Request failed ({e}) — skipping")
        return False, ""  # don't close on network errors


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Job Status Checker ===\n")

    ws = get_worksheet()

    # Read columns B (URL formula) and E (Status), starting from row 2
    b_col = ws.get("B2:B5000", value_render_option="FORMULA")
    e_col = ws.get("E2:E5000")

    updates = []
    checked = closed = skipped = 0

    for i, (b_row, e_row) in enumerate(zip(b_col, e_col)):
        sheet_row = i + 2  # 1-indexed; row 1 is header

        # Only check rows with Status = "Open"
        status = e_row[0].strip() if e_row else ""
        if status != "Open":
            continue

        # Extract LinkedIn URL from HYPERLINK formula
        b_val = b_row[0] if b_row else ""
        m = re.search(r'HYPERLINK\("([^"]+)"', str(b_val))
        if not m:
            continue

        url = m.group(1)

        # MBA-Exchange URLs require login — handled by scraper_mbaexchange.py
        if "mba-exchange.com" in url:
            continue

        print(f"  Checking row {sheet_row}: {url[:70]}...")
        checked += 1

        closed_flag, reason = is_closed(url)
        if closed_flag:
            updates.append({
                "range": f"E{sheet_row}",
                "values": [["Closed"]],
            })
            print(f"    → CLOSED ({reason})")
            closed += 1
        else:
            print(f"    → Still open")
            skipped += 1

        time.sleep(REQUEST_SLEEP)

    # Batch-write all status updates at once
    if updates:
        ws.batch_update(updates)
        print(f"\n✓ Marked {closed} jobs as Closed (out of {checked} checked).")
    else:
        print(f"\nAll {checked} checked jobs are still open.")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
