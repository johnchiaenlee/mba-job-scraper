# MBA Internship Job Scraper

> An automated pipeline that finds MBA summer internships in **Strategy, Operations, and Program Management**, filters out the noise with an LLM, and delivers a clean, deduplicated shortlist to a Google Sheet every morning.

![Python](https://img.shields.io/badge/python-3.11-blue) ![GitHub Actions](https://img.shields.io/badge/runs%20on-GitHub%20Actions-2088FF) ![Claude Haiku](https://img.shields.io/badge/LLM-Claude%20Haiku%204.5-D97757)

---

## 1. The Problem

MBA internship recruiting is a high-volume, time-sensitive search problem:

- **Signal-to-noise is terrible.** Searching "MBA intern" on LinkedIn returns senior roles, undergrad internships, PhD research posts, and healthcare administrative fellowships that look relevant but aren't.
- **Postings are scattered.** Roles live on LinkedIn *and* on school-gated boards like MBA-Exchange, and neither talks to the other.
- **Timing matters.** Early applicants have an edge, and roles close without notice.
- **Manual triage doesn't scale.** Checking 40+ companies and keywords every day by hand takes hours a week.

## 2. The Solution

A zero-touch daily pipeline that runs for free on GitHub Actions:

| User need | What the product does |
|---|---|
| "Only show me roles I'd actually apply to" | Two-layer filter: a free keyword pre-filter, then Claude Haiku checks every posting against strict MBA-targeting rules |
| "Don't make me check multiple sites" | Combines **LinkedIn** and **MBA-Exchange** into one Google Sheet |
| "Don't show me the same job twice" | Dedupes by URL, plus title + company matching across sources |
| "Tell me what matters at a glance" | Auto-tags function (Strategy / Ops / PGM), visa sponsorship, application deadline, and posting date |
| "Don't waste my time on dead links" | A weekly checker marks closed postings as `Closed` |

The result is a Google Sheet that refreshes before you wake up.

## 3. How It Works

```
                    ┌─────────────────────────┐
  5:00am PT daily → │  LinkedIn (JobSpy)      │──┐
                    │  ~44 keywords, last 25h │  │
                    └─────────────────────────┘  │     ┌──────────────────────┐     ┌──────────────────┐
                                                 ├───▶ │ Dedup vs. sheet      │ ──▶ │ Layer 1: title   │
                    ┌─────────────────────────┐  │     │ (URL + title/company)│     │ pre-filter (free)│
  5:30am PT daily → │  MBA-Exchange           │──┘     └──────────────────────┘     └────────┬─────────┘
                    │  (Playwright, logged-in)│                                              │
                    └─────────────────────────┘                                              ▼
                                                       ┌──────────────────────┐     ┌──────────────────┐
                                                       │   Google Sheet       │ ◀── │ Layer 2: Claude  │
                                                       │   (append new rows)  │     │ Haiku classifier │
                                                       └──────────▲───────────┘     └──────────────────┘
                                                                  │
                    Mondays 7:00am PT → check_closed.py ──────────┘  (Open → Closed)
```

### Two-layer quality filter

**Layer 1: Title pre-filter (free, instant).** The title must contain an internship signal (`intern`, `summer associate`, `rotational`, `fellowship`, `mba`, …) and none of the seniority or degree excludes (`senior`, `director`, `vp`, `principal`, `phd`, …). This removes the obvious misses before any API spend.

**Layer 2: LLM classifier (Claude Haiku 4.5).** The model reads the full posting and returns structured JSON:

```json
{ "function": ["Strategy", "Ops"], "sponsorship": "Not Specified", "deadline": "11/15/2026", "is_mba_targeted": true }
```

A role counts as MBA-targeted only if it is a **temporary internship** and is aimed at **MBA or general graduate** students. The classifier explicitly rejects full-time roles, undergrad-only internships, PhD and research roles, non-business MS programs, and healthcare administrative fellowships (MHA/MPH track). When it's unsure, it rejects. Precision matters more than recall here.

### Output schema (Google Sheet)

| Col | Field | Notes |
|---|---|---|
| A | Company | |
| B | Role Title | Clickable `HYPERLINK` to the posting |
| C | Function | `Strategy` / `Ops` / `PGM` (can be more than one) |
| D | Location | City, state |
| E | Status | `Open` / `Closed` / `Not MBA` |
| F | Sponsorship | `Yes` / `No` / `Not Specified` |
| G | Application Deadline | Pulled from the description when present |
| H | Posted | Actual post date (M/D/YYYY); falls back to the page's JSON-LD when the API omits it |

## 4. Repository Map

| File | Role | Schedule |
|---|---|---|
| [`job_scraper.py`](job_scraper.py) | **Core.** LinkedIn scrape → filter → append | Daily, 5:00am PT |
| [`scraper_mbaexchange.py`](scraper_mbaexchange.py) | Logged-in MBA-Exchange scrape with human-like browsing (stealth, random delays, natural typing and scrolling) | Daily, 5:30am PT |
| [`check_closed.py`](check_closed.py) | Re-checks `Open` rows and marks expired postings `Closed` (handles 404s, LinkedIn redirects, MBA-Exchange closed pages) | Weekly, Mon 7:00am PT |
| [`recheck_mba.py`](recheck_mba.py) | Re-runs the classifier on existing rows after prompt rules change | Manual |
| [`backfill_keywords_90d.py`](backfill_keywords_90d.py) | One-off 90-day lookback for newly added keywords | Manual |
| [`backfill_location_posted.py`](backfill_location_posted.py) | One-off fix to fill Location and Posted on older rows | Manual |
| [`fix_mbaex_urls.py`](fix_mbaex_urls.py) | One-off repair for malformed MBA-Exchange URLs | Manual |
| [`.github/workflows/`](.github/workflows) | One workflow per script; every one can also be triggered by hand | |

## 5. Getting Started

### Prerequisites
- A Google Cloud **service account** with the Sheets API enabled. Share your target Google Sheet with the service account's email as an Editor.
- An **Anthropic API key**.
- *(Optional)* MBA-Exchange credentials from your school.

### Setup
1. **Fork** this repo.
2. In every script, set `SHEET_ID` to your own Google Sheet's ID.
3. Add these under **Settings → Secrets and variables → Actions**:

   | Secret | Required for |
   |---|---|
   | `ANTHROPIC_API_KEY` | All LLM classification |
   | `GOOGLE_CREDENTIALS_JSON` | All scripts (paste the full service-account JSON) |
   | `MBA_EXCHANGE_EMAIL` | MBA-Exchange scraper only |
   | `MBA_EXCHANGE_PASSWORD` | MBA-Exchange scraper only |

4. Open the **Actions** tab, enable workflows, and use **Run workflow** on *Daily MBA Job Scraper* to test it.

### Run locally
```bash
pip install -r requirements.txt
playwright install chromium   # only needed for MBA-Exchange
export ANTHROPIC_API_KEY=...  GOOGLE_CREDENTIALS_JSON="$(cat service-account.json)"
python job_scraper.py
```

### Customize
- **Target roles:** edit `KEYWORDS_GENERIC` and `KEYWORDS_COMPANY` in `job_scraper.py`.
- **What counts as a fit:** edit `CLASSIFY_PROMPT`, then run `recheck_mba.py` to re-score existing rows.
- **Volume and rate limits:** `RESULTS_PER_KEYWORD`, `HOURS_OLD`, `SCRAPE_SLEEP_SEC`.

## 6. Product Decisions & Trade-offs

- **Broad input, strict filter.** Keywords are deliberately wide, including company-specific searches for target employers. The LLM handles precision, so a role that's titled oddly still gets found.
- **Cheap layer first.** The keyword pre-filter catches most irrelevant results at no cost, so the paid model only sees plausible candidates. Haiku was chosen for cost and speed on a high-volume classification task.
- **Default to "no".** A missed role costs less than a feed full of noise. Rejected rows can be audited, and `Not MBA` rows stay visible instead of being deleted.
- **Serverless by design.** GitHub Actions cron means nothing to host and nothing to maintain, and every workflow can also be run by hand for debugging.
- **The sheet is the UI.** Recruiting already happens in spreadsheets, so meeting users there beats building a new app.

## 7. Roadmap

- [ ] Move `SHEET_ID` and keyword lists into one shared config file (no more per-script edits)
- [ ] Extract shared helpers (Sheets client, classifier, pre-filter) into a common module
- [ ] Add sources: company career pages, Handshake, school job boards
- [ ] Daily digest email or Slack message with new high-fit roles
- [ ] Fit scoring that ranks roles against a resume, not just a yes/no filter
- [ ] Track the application funnel (Applied → Interview → Offer) in the same sheet
- [ ] Unit tests for the pre-filter and for parsing classifier output

## 8. Responsible Use

This is a personal job-search tool. It runs at low volume with built-in delays. Use the MBA-Exchange scraper only with your own credentials, and follow each site's terms of service. Never commit credentials; all secrets go through GitHub Actions Secrets.
