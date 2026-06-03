#!/usr/bin/env python3
"""
MBA-Exchange Job Scraper
========================
Logs into mba-exchange.com with your UCLA Anderson account,
scrapes the personalized internship job board, and appends
qualifying jobs to the same Google Sheet used by job_scraper.py.

Human-like behaviour to avoid detection:
  - playwright-stealth: removes Playwright/headless fingerprints
  - Random delays between all actions (variable, not fixed)
  - Character-by-character typing at human speed
  - Incremental scrolling with natural pauses
  - Realistic viewport, locale, and timezone

Required environment variables:
  MBA_EXCHANGE_EMAIL       — your mba-exchange.com login email
  MBA_EXCHANGE_PASSWORD    — your mba-exchange.com password
  ANTHROPIC_API_KEY        — Anthropic API key
  GOOGLE_CREDENTIALS_JSON  — Google service-account JSON (full JSON string)

Column layout written to Google Sheet (same as job_scraper.py):
  A = Company
  B = Role Title (=HYPERLINK formula)
  C = Function  (Strategy / Ops / PGM)
  D = Location
  E = Status    (Open)
  F = Sponsorship
  G = Application Deadline
  H = Posted    (M/D/YYYY)
"""

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright, Page, BrowserContext

try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("[warn] playwright-stealth not installed — bot fingerprints not suppressed")

# ── Config ────────────────────────────────────────────────────────────────────

SHEET_ID   = "1M5SaGYmAFZAbtxCYwDRz68jXddnBlcvuZNbhnSKFIg8"
SHEET_TAB  = "Sheet1"

LOGIN_URL  = "https://www.mba-exchange.com/candidates/Login.php"
SEARCH_URL = "https://www.mba-exchange.com/candidates/jobSearch_p.php"

LLM_SLEEP_SEC = 0.3

# ── Layer 1: title pre-filter (same rules as job_scraper.py) ──────────────────

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


def passes_title_prefilter(title: str) -> bool:
    t = title.lower()
    return (any(kw in t for kw in TITLE_INTERN_SIGNALS) and
            not any(kw in t for kw in TITLE_HARD_EXCLUDES))


# ── Layer 2: LLM classification (same prompt as job_scraper.py) ───────────────

CLASSIFY_PROMPT = """\
Analyze this job posting and return a JSON classification.

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

- deadline: Search the description carefully for any application deadline.
    Format as "M/D/YYYY". Return null ONLY if truly absent.

- is_mba_targeted: true ONLY if ALL of the following hold:
    (a) Role is temporary/internship (summer intern, co-op, fellowship,
        rotational, or similar) — NOT a permanent full-time hire.
    (b) Targets MBA students OR generic "graduate" without specifying
        a non-MBA degree.
    Set is_mba_targeted=false for:
    - Permanent full-time roles
    - Undergrad-only internships
    - PhD / doctoral candidates or academic researchers
    - MS students in non-business fields (Engineering, CS, Public Policy…)
    - Research assistant/fellow at think-tanks, universities, non-profits
    - Administrative Fellowships at healthcare/university/government orgs
      (target MHA/MPH, NOT MBA)
    - "MBA" appearing only incidentally (e.g. in company name)
    When in doubt, default to false.
"""


def classify_job(client: anthropic.Anthropic, title: str, company: str, desc: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(
            title=title, company=company,
            description=(desc or "")[:6000],
        )}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


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


def hyperlink(url: str, title: str) -> str:
    safe_title = title.replace('"', "'").replace("\n", " ")
    return f'=HYPERLINK("{url}","{safe_title}")'


def parse_date_str(raw: str) -> str:
    """
    Convert 'Jun, 02' / 'Jun 02' / 'Jun 02, 2026' etc. → M/D/YYYY.
    Falls back to today's date if parsing fails.
    """
    raw = raw.strip().rstrip(",").strip()
    today = datetime.utcnow()
    for fmt in ("%b, %d", "%b %d", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            # If year not in format, assume current year
            if "%Y" not in fmt:
                dt = dt.replace(year=today.year)
            return dt.strftime("%-m/%-d/%Y")
        except ValueError:
            continue
    # Return today as fallback
    return today.strftime("%-m/%-d/%Y")


# ── Human-like Playwright helpers ─────────────────────────────────────────────

async def human_sleep(min_s: float = 0.8, max_s: float = 2.2) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def human_type(page: Page, selector: str, text: str) -> None:
    """Click a field then type character by character at human speed."""
    await page.click(selector)
    await asyncio.sleep(random.uniform(0.3, 0.7))
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(0.04, 0.14))  # 40-140ms per key
    await asyncio.sleep(random.uniform(0.2, 0.5))


async def human_scroll_to_bottom(page: Page) -> None:
    """
    Gradually scroll the page in random increments until no new content loads.
    Mimics a human reading through results.
    """
    prev_count = 0
    stale_rounds = 0

    while stale_rounds < 3:
        # Scroll a random amount between 400-800px
        scroll_px = random.randint(400, 800)
        await page.mouse.wheel(0, scroll_px)
        await asyncio.sleep(random.uniform(1.2, 2.5))  # wait for content to load

        # Count loaded job cards
        cards = await page.query_selector_all("div.job-card, div[class*='jobCard'], article[class*='job']")
        count = len(cards)

        if count > prev_count:
            print(f"    Loaded {count} cards so far...")
            prev_count = count
            stale_rounds = 0
        else:
            stale_rounds += 1

        # Occasionally pause a bit longer (simulates reading)
        if random.random() < 0.2:
            await asyncio.sleep(random.uniform(1.0, 2.0))

    print(f"  Scroll complete — {prev_count} job cards loaded total")


# ── Login ─────────────────────────────────────────────────────────────────────

async def login(page: Page, email: str, password: str) -> bool:
    """
    Navigate to the job search URL — the site auto-redirects to Login.php
    if not authenticated. Fill in credentials and submit.
    Returns True on success, False if login failed.

    Login page confirmed:
      URL:     /candidates/Login.php
      Fields:  placeholder="Email address" / placeholder="Password"
      Button:  <button> with text "Log In"
    """
    print("  Navigating to search page (will redirect to login if needed)...")
    await page.goto(SEARCH_URL, wait_until="domcontentloaded")
    await human_sleep(1.5, 3.0)

    # Already logged in? (no redirect to Login.php)
    if "login" not in page.url.lower():
        print("  Already authenticated — skipping login")
        return True

    print(f"  Redirected to login: {page.url}")
    print("  Filling credentials...")

    # Confirmed selectors from screenshot
    await human_type(page, 'input[placeholder="Email address"]', email)
    await human_sleep(0.6, 1.3)
    await human_type(page, 'input[placeholder="Password"]', password)
    await human_sleep(0.8, 1.6)

    # Click the "Log In" button (big teal full-width button)
    await page.click('button:has-text("Log In")')
    await page.wait_for_load_state("networkidle", timeout=20000)
    await human_sleep(2.0, 3.5)

    if "login" in page.url.lower():
        print("[error] Still on login page — check credentials or account status")
        await page.screenshot(path="login_failed.png")
        return False

    print(f"  Logged in ✓  →  {page.url}")
    return True


async def _find_selector(page: Page, selectors: list[str]) -> str | None:
    """Return the first selector that finds a visible element, or None."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return sel
        except Exception:
            continue
    return None


# ── Job scraping ──────────────────────────────────────────────────────────────

async def scrape_job_cards(page: Page) -> list[dict]:
    """
    Parse all loaded job cards on the search results page.
    Returns list of dicts with: title, company, location, date, url, h1b
    """
    cards = await page.query_selector_all(
        "div.job-card, div[class*='jobCard'], div[class*='job-card'], article[class*='job']"
    )
    if not cards:
        # Fallback: try to find any card-like container with a View button
        cards = await page.query_selector_all("div:has(a:text('View'))")

    print(f"  Parsing {len(cards)} job cards...")
    jobs = []

    for card in cards:
        try:
            # Title — usually the most prominent text, inside a heading or link
            title = await _card_text(card, [
                "h3", "h4", "h2",
                "[class*='title']", "[class*='Title']",
                "a[href*='jobView']",
            ])

            # Company
            company = await _card_text(card, [
                "[class*='company']", "[class*='Company']",
                "[class*='employer']", "[class*='Employer']",
                "p strong", "strong",
            ])

            # Location
            location = await _card_text(card, [
                "[class*='location']", "[class*='Location']",
                "span[class*='loc']",
                # MBA-Exchange often shows "USA(Florida)" style
                "p:has-text('USA')", "span:has-text('USA')",
            ])

            # Date badge (top-left, format "Jun, 02")
            date_raw = await _card_text(card, [
                "[class*='date']", "[class*='Date']",
                "span.badge", ".badge", "small",
            ])
            posted = parse_date_str(date_raw) if date_raw else datetime.utcnow().strftime("%-m/%-d/%Y")

            # View URL
            url = ""
            view_link = await card.query_selector("a:has-text('View'), a[href*='jobView'], a[href*='job_view']")
            if view_link:
                href = await view_link.get_attribute("href")
                if href:
                    url = href if href.startswith("http") else f"https://www.mba-exchange.com{href}"

            # H1B sponsor badge
            h1b_el = await card.query_selector("[class*='sponsor'], [class*='Sponsor'], :has-text('H1B'), :has-text('H-1B')")
            h1b = "Yes" if h1b_el else "Not Specified"

            if title and url:
                jobs.append({
                    "title": title.strip(),
                    "company": company.strip() if company else "",
                    "location": location.strip() if location else "USA",
                    "posted": posted,
                    "url": url,
                    "h1b": h1b,
                })
        except Exception as e:
            print(f"    [warn] Error parsing card: {e}")
            continue

    return jobs


async def _card_text(card, selectors: list[str]) -> str:
    """Try each selector on a card element and return the first non-empty text."""
    for sel in selectors:
        try:
            el = await card.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


async def get_job_description(page: Page, url: str) -> str:
    """
    Visit the job detail page and extract the description text.
    Uses a fresh navigation (not a new tab) with human delays.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await human_sleep(1.5, 3.0)

        # Try common description containers
        desc = await _page_text(page, [
            "[class*='description']", "[class*='Description']",
            "[class*='job-detail']", "[class*='jobDetail']",
            "article", "main", ".content",
        ])
        return desc[:8000] if desc else ""
    except Exception as e:
        print(f"    [warn] Failed to load detail page: {e}")
        return ""


async def _page_text(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if len(text) > 100:  # must be substantive
                    return text
        except Exception:
            continue
    return ""


# ── Main ──────────────────────────────────────────────────────────────────────

async def async_main() -> None:
    print(f"\n=== MBA-Exchange Scraper  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===\n")

    email    = os.environ["MBA_EXCHANGE_EMAIL"]
    password = os.environ["MBA_EXCHANGE_PASSWORD"]

    # 1. Load existing sheet URLs for dedup
    print("Connecting to Google Sheets...")
    ws = get_worksheet()
    existing_urls = get_existing_urls(ws)
    print(f"Existing jobs in sheet: {len(existing_urls)}\n")

    llm_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    async with async_playwright() as pw:
        # Launch Chromium — realistic viewport + locale
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",  # hide automation flag
            ],
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        page = await context.new_page()

        # Apply stealth patches if available
        if HAS_STEALTH:
            await stealth_async(page)
            print("Stealth mode: ON")

        # 2. Login (navigates to SEARCH_URL, handles redirect to Login.php if needed)
        print("Logging in to mba-exchange.com...")
        success = await login(page, email, password)
        if not success:
            print("[error] Login failed — aborting")
            await browser.close()
            return

        # If login redirected us away from the search page, go back
        if "jobSearch" not in page.url:
            print(f"\nNavigating to job search: {SEARCH_URL}")
            await page.goto(SEARCH_URL, wait_until="domcontentloaded")
            await human_sleep(2.0, 3.5)
        else:
            print(f"\nAlready on job search page ✓")
            await human_sleep(1.0, 2.0)

        # Wait for job cards to appear
        try:
            await page.wait_for_selector(
                "div.job-card, div[class*='jobCard'], a:has-text('View')",
                timeout=15000,
            )
        except Exception:
            print("[warn] Job cards did not appear within timeout — proceeding anyway")
            await page.screenshot(path="search_debug.png")

        # 4. Scroll to load all results
        print("\nScrolling to load all job cards...")
        await human_scroll_to_bottom(page)

        # 5. Parse all cards
        print("\nParsing job cards...")
        jobs = await scrape_job_cards(page)
        print(f"Total cards parsed: {len(jobs)}")

        if not jobs:
            print("No jobs found — check selectors or login state")
            await page.screenshot(path="no_jobs_debug.png")
            await browser.close()
            return

        # 6. Deduplicate against existing sheet
        new_jobs = [j for j in jobs if j["url"] and j["url"] not in existing_urls]
        # Also dedup within batch (same title+company)
        seen = set()
        deduped = []
        for j in new_jobs:
            key = f"{j['title'].lower()}|{j['company'].lower()}"
            if key not in seen:
                seen.add(key)
                deduped.append(j)
        new_jobs = deduped

        print(f"New (not in sheet): {len(new_jobs)}\n")

        if not new_jobs:
            print("Sheet already up to date.")
            await browser.close()
            return

        # 7. Filter, classify, and build rows
        rows    = []
        skipped = 0

        for job in new_jobs:
            title   = job["title"]
            company = job["company"]
            url     = job["url"]

            # Layer 1: title pre-filter (free)
            if not passes_title_prefilter(title):
                print(f"  [L1 skip] {title}")
                skipped += 1
                continue

            # Fetch detail page for description (required for LLM)
            print(f"  Fetching detail: {title} @ {company}")
            desc = await get_job_description(page, url)

            # Go back to search results isn't needed — we collected all card data already
            # (We navigate directly to each URL, then classify)

            # Layer 2: LLM classification
            print(f"  Classifying...")
            try:
                result = classify_job(llm_client, title, company, desc)
            except Exception as e:
                print(f"    [error] LLM failed ({e}) — skipping")
                skipped += 1
                # Navigate back for next job
                await page.goto(SEARCH_URL, wait_until="domcontentloaded")
                await human_sleep(1.5, 2.5)
                continue

            if not result.get("is_mba_targeted", True):
                print(f"    → [L2 skip] not MBA-targeted")
                skipped += 1
                await human_sleep(0.3, 0.8)
                continue

            sponsorship = result.get("sponsorship", "Not Specified")
            # MBA-Exchange shows H1B badge on card — use it as a signal
            if job["h1b"] == "Yes" and sponsorship == "Not Specified":
                sponsorship = "Yes"

            rows.append([
                company,
                hyperlink(url, title),
                ", ".join(result.get("function", [])),
                job["location"],
                "Open",
                sponsorship,
                result.get("deadline") or "",
                job["posted"],
            ])
            print(f"    ✓ Added: {title} @ {company}  [{job['posted']}]")

            await asyncio.sleep(LLM_SLEEP_SEC)

        await browser.close()

    # 8. Write to sheet
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"\n✓ Added {len(rows)} new jobs from MBA-Exchange to '{SHEET_TAB}'.")
    else:
        print("\nNo qualifying MBA jobs found to add.")

    print(f"  Skipped: {skipped}")
    print("\nDone.\n")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
