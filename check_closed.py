#!/usr/bin/env python3
"""
Weekly job-status checker.

For every row in the sheet where Status = "Open", ping the LinkedIn job URL.
If the job is no longer available, update Status → "Closed".

Signals that a job is closed:
  - HTTP 404
  - LinkedIn redirects away from /jobs/view/ (e.g. ?trk=expired_jd_redirect)
  - Response contains "No longer accepting applications"
  - Response contains "This job is no longer available"
  - Response contains "job has expired"

Rate-limited / errored checks (429, 999, 5xx, network) are retried, then
counted as "unknown" and left Open. The run fails if too many are unknown,
so a blocked run can't pass silently.
"""

import json
import os
import re
import sys
import time

import gspread
import requests
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

SHEET_ID       = "1M5SaGYmAFZAbtxCYwDRz68jXddnBlcvuZNbhnSKFIg8"
SHEET_TAB      = "Sheet1"
REQUEST_SLEEP  = 3      # seconds between URL checks (be polite to LinkedIn)
REQUEST_TIMEOUT = 10    # seconds
RETRY_STATUSES  = {429, 999, 500, 502, 503, 504}  # 999 = LinkedIn bot block
MAX_RETRIES     = 3
RETRY_BACKOFF   = 30    # seconds, multiplied by attempt number
MAX_UNKNOWN_PCT = 10    # fail the run if more than this % couldn't be checked

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

# LinkedIn redirects removed jobs away from /jobs/view/XXX — originally to
# /jobs/search/?currentJobId=XXX, now to /jobs/<keyword>-jobs?trk=expired_jd_redirect
LINKEDIN_JOB_PATH = "/jobs/view/"
LINKEDIN_CLOSED_REDIRECTS = ["/jobs/search/", "trk=expired_jd_redirect"]

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


def check_status(url: str) -> tuple[str, str]:
    """
    Return (status, reason) for a job URL, where status is
    "closed", "open", or "unknown" (couldn't get a usable response).

    Detection signals:
      1. HTTP 404
      2. LinkedIn redirect to a job-search page (job removed); any other
         redirect away from /jobs/view/ (e.g. sign-in wall) is "unknown"
      3. Page body contains known "closed" phrases
    """
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            time.sleep(RETRY_BACKOFF * attempt)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                                allow_redirects=True)
        except Exception as e:
            reason = f"request failed ({e})"
            continue
        if resp.status_code in RETRY_STATUSES:
            reason = f"HTTP {resp.status_code}"
            continue
        break
    else:
        return "unknown", reason

    # Signal 1: 404
    if resp.status_code == 404:
        return "closed", "HTTP 404"

    # Signal 2: LinkedIn redirected away from the job page
    if LINKEDIN_JOB_PATH in url and LINKEDIN_JOB_PATH not in resp.url:
        if any(r in resp.url for r in LINKEDIN_CLOSED_REDIRECTS):
            return "closed", f"redirected → {resp.url[:80]}"
        return "unknown", f"unexpected redirect → {resp.url[:80]}"

    # Signal 3: Closed phrases in body
    body = resp.text.lower()
    for phrase in CLOSED_PHRASES:
        if phrase in body:
            return "closed", f'phrase: "{phrase}"'

    if resp.status_code != 200:
        return "unknown", f"HTTP {resp.status_code}"
    return "open", ""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Job Status Checker ===\n")

    ws = get_worksheet()

    # Read columns B (URL formula) and E (Status), starting from row 2
    b_col = ws.get("B2:B5000", value_render_option="FORMULA")
    e_col = ws.get("E2:E5000")

    updates = []
    checked = closed = skipped = unknown = 0

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

        status, reason = check_status(url)
        if status == "closed":
            updates.append({
                "range": f"E{sheet_row}",
                "values": [["Closed"]],
            })
            print(f"    → CLOSED ({reason})")
            closed += 1
        elif status == "unknown":
            print(f"    → UNKNOWN ({reason}) — left Open")
            unknown += 1
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

    print(f"Summary: {checked} checked | {closed} closed | {skipped} open | {unknown} unknown")
    if checked and unknown * 100 / checked > MAX_UNKNOWN_PCT:
        print(f"\n[error] {unknown}/{checked} jobs couldn't be checked "
              f"(> {MAX_UNKNOWN_PCT}%) — likely rate-limited. Failing run.")
        sys.exit(1)

    print("\nDone.\n")


if __name__ == "__main__":
    main()
