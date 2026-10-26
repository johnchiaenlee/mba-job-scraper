#!/usr/bin/env python3
"""
Regression check on REAL LinkedIn postings: do the new safeguards drop jobs the
old version kept (or keep jobs it rejected)?

Scrapes a few live keywords, then classifies each posting with the old and the
new job_scraper.py (each with its own prompt and keep/skip rule) and lists
every disagreement for manual review. Never writes to the sheet.

Usage (needs ANTHROPIC_API_KEY):
  python eval/regression_real.py OLD_job_scraper.py NEW_job_scraper.py
"""

import importlib.util
import os
import sys
import time

import anthropic
import pandas as pd

KEYWORDS = ["MBA Strategy Intern", "MBA Operations Intern",
            "MBA Program Manager Intern", "MBA Summer Associate"]
MAX_POSTINGS = 50


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    old, new = load(sys.argv[1], "old_js"), load(sys.argv[2], "new_js")
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    frames = []
    for kw in KEYWORDS:
        frames.append(new.scrape_jobs(site_name=["linkedin"], search_term=kw, location="United States",
                                      results_wanted=15, hours_old=72, linkedin_fetch_description=True))
        time.sleep(5)
    jobs = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["job_url"])
    jobs = jobs[jobs["title"].map(lambda t: new.passes_title_prefilter(new.safe(t)))].head(MAX_POSTINGS)

    agree = old_keep = new_keep = 0
    diffs = []
    for _, job in jobs.iterrows():
        title, company, desc = new.safe(job.get("title")), new.safe(job.get("company")), new.safe(job.get("description"))
        try:
            r_old = old.classify_job(client, title, company, desc)
            keep_old = bool(r_old.get("is_mba_targeted", True))
        except Exception as e:
            keep_old, r_old = False, {"error": str(e)[:80]}
        try:
            r_new = new.classify_job(client, title, company, desc)
            clean, reason = new.validate_result(r_new, title, desc)
            keep_new = clean is not None
        except Exception as e:
            keep_new, reason, r_new = False, f"error {str(e)[:80]}", {}
        old_keep += keep_old
        new_keep += keep_new
        if keep_old == keep_new:
            agree += 1
        else:
            diffs.append((title, company, keep_old, keep_new, reason, r_new.get("evidence")))
        time.sleep(0.3)

    n = len(jobs)
    print("\n" + "=" * 70 + f"\nREGRESSION ON {n} REAL POSTINGS\n" + "=" * 70)
    print(f"Old kept: {old_keep}   New kept: {new_keep}   Same decision: {agree}/{n} ({agree / n:.0%})")
    for title, company, ko, kn, reason, ev in diffs:
        print(f"\n  {'OLD keep → NEW skip' if ko else 'OLD skip → NEW keep'}: {title} @ {company}")
        print(f"    new reason: {reason or '-'} | evidence: {str(ev)[:90]!r}")


if __name__ == "__main__":
    main()
