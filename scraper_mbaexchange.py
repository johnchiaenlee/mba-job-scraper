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
import sys
import time
from datetime import datetime

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright, Page, BrowserContext

# playwright-stealth v1 vs v2 API compatibility
try:
    from playwright_stealth import stealth_async   # v1 API
    HAS_STEALTH = True
except ImportError:
    try:
        from playwright_stealth import Stealth      # v2 API
        async def stealth_async(page):              # shim to v2
            await Stealth().apply_stealth_async(page)
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


def get_existing_sheet_data(ws: gspread.Worksheet) -> tuple[set, set]:
    """
    Read existing sheet rows and return two dedup sets:
      existing_urls — job URLs already in sheet (catches same-source duplicates)
      existing_keys — "title_lower|company_lower" pairs (catches cross-source
                      duplicates, e.g. same job on LinkedIn AND MBA-Exchange)
    """
    try:
        a_col = ws.get("A2:A5000")                               # company
        b_col = ws.get("B2:B5000", value_render_option="FORMULA")  # HYPERLINK
    except Exception as e:
        print(f"[warn] Could not read existing sheet data: {e}")
        return set(), set()

    existing_urls: set = set()
    existing_keys: set = set()

    for i, b_row in enumerate(b_col):
        if not b_row:
            continue
        cell = str(b_row[0])

        # Extract URL from =HYPERLINK("url","title")
        m_url = re.search(r'HYPERLINK\("([^"]+)"', cell)
        if m_url:
            existing_urls.add(m_url.group(1))

        # Extract title (second argument of HYPERLINK)
        m_title = re.search(r'HYPERLINK\("[^"]+","([^"]+)"', cell)
        title = m_title.group(1).strip() if m_title else cell.strip()

        # Company from column A
        company = (a_col[i][0].strip() if i < len(a_col) and a_col[i] else "")

        if title and company:
            existing_keys.add(f"{title.lower()}|{company.lower()}")

    return existing_urls, existing_keys


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


async def human_scroll_to_bottom(page: Page) -> int:
    """
    Gradually scroll #lstJobs until no new div.job-instructor-layout cards appear.
    Returns the final card count.

    Confirmed HTML structure:
      div#lstJobs
        └─ div.col-xl-3... (wrapper per card)
             └─ div.job-instructor-layout  ← the actual card
    """
    # Read expected total from the counter badge
    try:
        total_el = await page.query_selector("span#nbJobsResult")
        total_text = (await total_el.inner_text()).strip() if total_el else ""
        expected = int(re.sub(r"[^\d]", "", total_text)) if total_text else None
        if expected:
            print(f"  Expecting {expected} job cards total")
    except Exception:
        expected = None

    prev_count = 0
    stale_rounds = 0

    while stale_rounds < 3:
        scroll_px = random.randint(450, 850)
        await page.mouse.wheel(0, scroll_px)
        await asyncio.sleep(random.uniform(1.2, 2.5))

        cards = await page.query_selector_all("div.job-instructor-layout")
        count = len(cards)

        if count > prev_count:
            print(f"    Loaded {count}/{expected or '?'} cards...")
            prev_count = count
            stale_rounds = 0
        else:
            stale_rounds += 1

        # If we've hit the expected total, stop early
        if expected and count >= expected:
            print(f"    All {count} cards loaded ✓")
            break

        if random.random() < 0.2:
            await asyncio.sleep(random.uniform(1.0, 2.0))

    print(f"  Scroll complete — {prev_count} job cards loaded")
    return prev_count


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

    # Wait for the page to fully render (Login.php uses JS to render the form)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass  # proceed even if networkidle doesn't settle
    await human_sleep(1.5, 2.5)

    # Screenshot for debugging (saved as artifact if run fails)
    await page.screenshot(path="login_page.png")
    print("  Saved login_page.png for debugging")

    # Confirmed field IDs from debug run (2026-06-03):
    #   visible email:    input#sLogEmail   (name="sLogEmail")
    #   visible password: input#sLogPass    (name="sLogPass")
    #   hidden duplicates: #emailAddress, #loginPassword — skip those
    email_sel = None
    for sel in [
        "input#sLogEmail",           # confirmed visible field
        'input[name="sLogEmail"]',
        'input[type="email"]:visible',
    ]:
        try:
            el = await page.wait_for_selector(sel, timeout=5000)
            if el and await el.is_visible():
                email_sel = sel
                print(f"  Found email field: {sel}")
                break
        except Exception:
            continue

    if not email_sel:
        print("[error] Could not find email input — check login_page.png artifact")
        return False

    pwd_sel = None
    for sel in [
        "input#sLogPass",            # confirmed visible field
        'input[name="sLogPass"]',
        'input[type="password"]:visible',
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                pwd_sel = sel
                print(f"  Found password field: {sel}")
                break
        except Exception:
            continue

    if not pwd_sel:
        print("[error] Could not find password input")
        return False

    print("  Filling credentials...")
    await human_type(page, email_sel, email)
    await human_sleep(0.6, 1.3)
    await human_type(page, pwd_sel, password)
    await human_sleep(0.8, 1.6)

    # Submit: press Enter from the password field (most reliable cross-browser)
    # The visible login form uses id="sLogPass" — Enter submits its parent form
    await page.locator(pwd_sel).press("Enter")
    print("  Submitted via Enter key")

    await page.wait_for_load_state("domcontentloaded", timeout=20000)
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
    Returns list of dicts: title, company, location, date, url, h1b

    Confirmed HTML structure (from DevTools inspection):
      div#lstJobs
        └─ div.col-xl-3.col-lg-4... (grid wrapper)
             └─ div.job-instructor-layout          ← one card per job
                  ├─ div.left-tags-capt            ← date badge ("Jun, 02")
                  ├─ div.brows-job-type            ← job type
                  ├─ div.job-instructor-thumb      ← company logo
                  ├─ div.job-instructor-content
                  │    ├─ div.jbs-job-employer-wrap   ← company name
                  │    ├─ p.h4.instructor-title       ← job title
                  │    ├─ div.text-center.text-sm-muted ← location
                  │    └─ div.jbs-grid-job-edrs-group   ← tags (H1B, etc.)
                  └─ div.jbs-grid-job-apply-btns a  ← "View" link
    """
    cards = await page.query_selector_all("div.job-instructor-layout")
    print(f"  Parsing {len(cards)} job cards...")
    jobs = []

    for card in cards:
        try:
            # ── Date (top-left badge, e.g. "Jun, 02") ─────────────────────────
            date_raw = await _el_text(card, "div.left-tags-capt")
            posted = parse_date_str(date_raw) if date_raw else datetime.utcnow().strftime("%-m/%-d/%Y")

            # ── Company name ───────────────────────────────────────────────────
            company = await _el_text(card, "div.jbs-job-employer-wrap")

            # ── Job title ──────────────────────────────────────────────────────
            title = await _el_text(card, "p.instructor-title")

            # ── Location (e.g. "USA(Florida)", "USA") ─────────────────────────
            location = await _el_text(card, "div.text-center.text-sm-muted")

            # ── H1B sponsor badge ──────────────────────────────────────────────
            edrs_text = await _el_text(card, "div.jbs-grid-job-edrs-group")
            h1b = "Yes" if "h1b" in (edrs_text or "").lower() or "h-1b" in (edrs_text or "").lower() else "Not Specified"

            # ── View URL ───────────────────────────────────────────────────────
            url = ""
            view_el = await card.query_selector("div.jbs-grid-job-apply-btns a")
            if view_el:
                href = await view_el.get_attribute("href")
                if href:
                    url = href if href.startswith("http") else f"https://www.mba-exchange.com{href}"

            if title and url:
                jobs.append({
                    "title": title.strip(),
                    "company": (company or "").strip(),
                    "location": (location or "USA").strip(),
                    "posted": posted,
                    "url": url,
                    "h1b": h1b,
                })
            else:
                print(f"    [warn] Skipped card — missing title or URL (title={title!r})")

        except Exception as e:
            print(f"    [warn] Error parsing card: {e}")
            continue

    return jobs


async def _el_text(parent, selector: str) -> str:
    """Return inner text of the first matching child element, or ''."""
    try:
        el = await parent.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return ""


async def get_job_description(page: Page, url: str) -> str:
    """
    Visit the MBA-Exchange job detail page and extract the description text.
    Tries MBA-Exchange-specific containers first, then falls back to <main>/<body>.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await human_sleep(1.5, 3.0)

        # MBA-Exchange detail page selectors (try specific → broad)
        for sel in [
            "div.job-description",
            "div.jbs-job-description",
            "div[class*='job-desc']",
            "div[class*='jobDesc']",
            "div.tab-content",
            "div.single-instructor-details",
            "main",
            "div#content",
            "body",
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if len(text) > 150:
                        return text[:8000]
            except Exception:
                continue
    except Exception as e:
        print(f"    [warn] Failed to load detail page: {e}")
    return ""


# ── MBA-Exchange closed-job checker ──────────────────────────────────────────

MBAEX_CLOSED_PHRASES = [
    "the job is no more available",          # confirmed from screenshot
    "job is no longer available",
    "position has been filled",
    "no longer accepting applications",
]


async def check_mbaexchange_closed(page: Page, ws: gspread.Worksheet) -> list[dict]:
    """
    Using the already logged-in Playwright session, visit each existing
    Open MBA-Exchange row and check if the job has been removed.

    Returns a list of gspread batch_update dicts for rows to mark Closed.
    """
    # Read Status (E) and URL (B formula) columns
    try:
        b_col = ws.get("B2:B5000", value_render_option="FORMULA")
        e_col = ws.get("E2:E5000")
    except Exception as ex:
        print(f"  [warn] Could not read sheet for closed check: {ex}")
        return []

    updates = []
    checked = 0

    for i, (b_row, e_row) in enumerate(zip(b_col, e_col)):
        sheet_row = i + 2
        status = (e_row[0].strip() if e_row else "")
        if status != "Open":
            continue

        b_val = str(b_row[0]) if b_row else ""
        m = re.search(r'HYPERLINK\("([^"]+)"', b_val)
        if not m:
            continue

        url = m.group(1)
        if "mba-exchange.com" not in url:
            continue  # LinkedIn URLs handled by check_closed.py

        print(f"  Checking row {sheet_row}: {url[:70]}...")
        checked += 1

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await human_sleep(1.0, 2.0)
            body = (await page.content()).lower()
            if any(phrase in body for phrase in MBAEX_CLOSED_PHRASES):
                updates.append({"range": f"E{sheet_row}", "values": [["Closed"]]})
                print(f"    → CLOSED")
            else:
                print(f"    → Still open")
        except Exception as ex:
            print(f"    [warn] Could not load {url}: {ex}")

    print(f"  MBA-Exchange closed check: {checked} checked, {len(updates)} closed")
    return updates


# ── Main ──────────────────────────────────────────────────────────────────────

async def async_main() -> None:
    print(f"\n=== MBA-Exchange Scraper  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===\n")

    email    = os.environ["MBA_EXCHANGE_EMAIL"]
    password = os.environ["MBA_EXCHANGE_PASSWORD"]

    # 1. Load existing sheet data for dedup (URL + title|company)
    print("Connecting to Google Sheets...")
    ws = get_worksheet()
    existing_urls, existing_keys = get_existing_sheet_data(ws)
    print(f"Existing jobs in sheet: {len(existing_urls)} (URL dedup), {len(existing_keys)} (title+company dedup)\n")

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
            sys.exit(1)   # non-zero so GitHub Actions marks the run as failed

        # If login redirected us away from the search page, go back
        if "jobSearch" not in page.url:
            print(f"\nNavigating to job search: {SEARCH_URL}")
            await page.goto(SEARCH_URL, wait_until="domcontentloaded")
            await human_sleep(2.0, 3.5)
        else:
            print(f"\nAlready on job search page ✓")
            await human_sleep(1.0, 2.0)

        # Wait for job cards to appear (confirmed selector: div.job-instructor-layout)
        try:
            await page.wait_for_selector("div.job-instructor-layout", timeout=20000)
            print("  Job cards detected ✓")
        except Exception:
            print("[warn] Job cards did not appear within timeout — check login or selectors")
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

        # 6. Three-layer dedup:
        #    (a) URL match — same source duplicate
        #    (b) title+company match vs existing sheet rows — cross-source duplicate
        #        (same job already added from LinkedIn with a different URL)
        #    (c) title+company match within current batch — MBA-Exchange internal dup
        seen_keys: set = set()
        deduped = []
        for j in jobs:
            if not j["url"]:
                continue
            key = f"{j['title'].lower()}|{j['company'].lower()}"
            if j["url"] in existing_urls:
                continue                          # (a) URL already in sheet
            if key in existing_keys:
                print(f"  [dedup] Already in sheet (diff source): {j['title']} @ {j['company']}")
                continue                          # (b) same job from LinkedIn
            if key in seen_keys:
                continue                          # (c) dup within this batch
            seen_keys.add(key)
            deduped.append(j)
        new_jobs = deduped

        print(f"New after dedup (URL + title+company): {len(new_jobs)}\n")

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

        # 8b. Check existing Open MBA-Exchange rows for closure
        # Reuse the logged-in Playwright session before closing the browser.
        print("\nChecking existing Open MBA-Exchange rows for closure...")
        close_updates = await check_mbaexchange_closed(page, ws)

        await browser.close()

    # 8. Write new jobs to sheet
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"\n✓ Added {len(rows)} new jobs from MBA-Exchange to '{SHEET_TAB}'.")
    else:
        print("\nNo qualifying MBA jobs found to add.")

    # 9. Write closed-job updates
    if close_updates:
        ws.batch_update(close_updates)
        print(f"✓ Marked {len(close_updates)} MBA-Exchange jobs as Closed.")

    print(f"  Skipped (new jobs): {skipped}")
    print("\nDone.\n")


def main() -> None:
    try:
        asyncio.run(async_main())
    except Exception as e:
        print(f"[fatal] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
