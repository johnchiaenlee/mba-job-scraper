#!/usr/bin/env python3
"""
One-time fix: repair malformed MBA-Exchange URLs in the Google Sheet.

Bug: scraper stored URLs like:
  https://www.mba-exchange.comJobDetail_p.php?sID=xxx   ← missing /candidates/

Correct URL:
  https://www.mba-exchange.com/candidates/JobDetail_p.php?sID=xxx

Scans all HYPERLINK formulas in column B and rewrites any malformed ones.
"""

import json
import os
import re

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID  = "1M5SaGYmAFZAbtxCYwDRz68jXddnBlcvuZNbhnSKFIg8"
SHEET_TAB = "Sheet1"

BAD_PREFIX  = "https://www.mba-exchange.comJobDetail_p.php"
GOOD_PREFIX = "https://www.mba-exchange.com/candidates/JobDetail_p.php"


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


def main():
    print("\n=== Fix malformed MBA-Exchange URLs ===\n")

    ws = get_worksheet()
    formulas = ws.get("B2:B5000", value_render_option="FORMULA")

    updates = []
    for i, row in enumerate(formulas):
        if not row:
            continue
        cell = str(row[0])
        if BAD_PREFIX not in cell:
            continue

        sheet_row = i + 2
        fixed_cell = cell.replace(BAD_PREFIX, GOOD_PREFIX)
        updates.append({"range": f"B{sheet_row}", "values": [[fixed_cell]]})
        print(f"  Row {sheet_row}: fixed URL")

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        print(f"\n✓ Fixed {len(updates)} URLs.")
    else:
        print("No malformed URLs found.")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
