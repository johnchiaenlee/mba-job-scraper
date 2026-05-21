#!/usr/bin/env python3
"""
One-time MBA re-validation pass.

Reads all rows where Status = "Open", re-runs Claude Haiku on each
using title + company (description not stored in sheet), and marks
any non-MBA-targeted row as "Not MBA" so it's easy to review/delete.

Run once after tightening the is_mba_targeted prompt rules.
"""

import json
import os
import re
import time

import anthropic
import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

SHEET_ID      = "1M5SaGYmAFZAbtxCYwDRz68jXddnBlcvuZNbhnSKFIg8"
SHEET_TAB     = "Sheet1"
LLM_SLEEP_SEC = 0.3

RECHECK_PROMPT = """\
Given only the job title and company name, decide if this is an MBA-targeted internship.

Title: {title}
Company: {company}

Return ONLY valid JSON — no markdown, no explanation:
{{"is_mba_targeted": true}}

Rules for is_mba_targeted:
- true if: the title/company strongly suggests an MBA internship or general graduate intern role.
    Acceptable: "MBA Intern", "MBA Strategy Intern", "Graduate Intern", "Summer Associate (MBA)",
    "Rotational Program", "MBA Fellow", or any internship title with no degree specified.
- false if ANY of the following:
    - Role targets PhD / doctoral candidates or academic researchers
    - Role targets MS students in non-business fields (Engineering, CS, Data Science,
      Public Policy, Sciences, etc.)
    - Role is research-focused with no business component (research assistant, research fellow,
      policy research, literature review roles at think-tanks, universities, non-profits)
    - Role is clearly undergrad-only (no graduate mention)
    - Role is permanent full-time (no intern/associate/fellow/rotational signal)
    - "MBA" appears only incidentally (in company name, unrelated context)
When in doubt, return false.
"""

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


def check_mba(client: anthropic.Anthropic, title: str, company: str) -> bool:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": RECHECK_PROMPT.format(
            title=title, company=company
        )}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw).get("is_mba_targeted", False)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== MBA Re-validation Pass ===\n")

    ws = get_worksheet()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Read columns A (company), B (title/link), E (status)
    a_col = ws.get("A2:A5000")
    b_col = ws.get("B2:B5000", value_render_option="FORMULA")
    e_col = ws.get("E2:E5000")

    updates = []
    checked = flagged = skipped = 0

    for i, (a_row, b_row, e_row) in enumerate(zip(a_col, b_col, e_col)):
        sheet_row = i + 2

        status = (e_row[0] if e_row else "").strip()
        if status != "Open":
            continue

        company = a_row[0].strip() if a_row else ""
        b_val   = b_row[0] if b_row else ""

        # Extract title from HYPERLINK formula, or use raw text
        m = re.search(r'HYPERLINK\("[^"]+","([^"]+)"', str(b_val))
        title = m.group(1) if m else str(b_val).strip()

        if not title or not company:
            continue

        print(f"  Row {sheet_row}: {title[:60]} @ {company[:30]}")
        checked += 1

        try:
            is_mba = check_mba(client, title, company)
        except Exception as e:
            print(f"    [error] {e} — skipping")
            skipped += 1
            continue

        if not is_mba:
            updates.append({"range": f"E{sheet_row}", "values": [["Not MBA"]]})
            print(f"    → NOT MBA — marked")
            flagged += 1
        else:
            print(f"    → OK")

        time.sleep(LLM_SLEEP_SEC)

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        print(f"\n✓ Flagged {flagged} non-MBA rows as 'Not MBA' (out of {checked} checked).")
    else:
        print(f"\nAll {checked} rows passed MBA check.")

    if skipped:
        print(f"  Skipped due to errors: {skipped}")
    print("\nDone.\n")


if __name__ == "__main__":
    main()
