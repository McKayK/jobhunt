"""Aggregate sources: Indeed, ZipRecruiter, Google Jobs via python-jobspy.

This is what finds jobs at *any* company near your ZIP, not just the ones on
your curated list. JobSpy scrapes each site's public search with a location +
radius, so the sites do the "who's hiring near me" discovery for us.

Notes:
- Each site is scraped independently and failures are per-site, so Indeed
  having a bad day doesn't kill ZipRecruiter results.
- If include-keywords are set, we run one search per keyword (a blank search
  plus query-time filtering would miss things the sites never returned).
  With no keywords we run a single blank search: "everything near me".
- LinkedIn/Glassdoor are deliberately off: they aggressively block scrapers
  and mostly duplicate Indeed anyway.
"""
from __future__ import annotations

from typing import Optional

SITE_SETTING_MAP = {
    "indeed": "use_indeed",
    "zip_recruiter": "use_zip_recruiter",
    "google": "use_google",
}


def _clean(val) -> Optional[str]:
    """pandas gives us NaN for missing values; normalize to None."""
    if val is None:
        return None
    try:
        import math
        if isinstance(val, float) and math.isnan(val):
            return None
    except Exception:
        pass
    s = str(val).strip()
    return s or None


def fetch_aggregate(sites: list[str], home_zip: str, radius_miles: float,
                    search_terms: list[str], results_wanted: int = 100,
                    hours_old: int = 0, on_progress=None) -> tuple[list[dict], list[str]]:
    """Scrape the given sites around home_zip. Returns (jobs, errors)."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        return [], ["python-jobspy is not installed — run: pip install python-jobspy --no-deps"]

    terms = [t for t in (search_terms or []) if t.strip()] or [""]
    jobs: list[dict] = []
    errors: list[str] = []

    for site in sites:
        for term in terms:
            label = f"{site}" + (f" '{term}'" if term else "")
            if on_progress:
                on_progress(f"Searching {label}…")
            try:
                kwargs = dict(
                    site_name=[site],
                    location=home_zip,
                    distance=int(round(radius_miles)),
                    results_wanted=results_wanted,
                    country_indeed="USA",
                    description_format="markdown",
                    verbose=0,
                )
                if term:
                    kwargs["search_term"] = term
                    # Google ignores location params unless baked into the query.
                    kwargs["google_search_term"] = f"{term} jobs near {home_zip} within {int(radius_miles)} miles"
                else:
                    kwargs["google_search_term"] = f"jobs near {home_zip} within {int(radius_miles)} miles"
                if hours_old and hours_old > 0:
                    kwargs["hours_old"] = hours_old

                df = scrape_jobs(**kwargs)
            except Exception as e:
                errors.append(f"{label}: {type(e).__name__}: {str(e)[:120]}")
                continue

            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                r = row.to_dict()
                title = _clean(r.get("title"))
                url = _clean(r.get("job_url"))
                company = _clean(r.get("company")) or "Unknown company"
                if not title or not url:
                    continue
                loc = _clean(r.get("location"))
                is_remote = bool(r.get("is_remote")) if r.get("is_remote") is not None else False
                if is_remote and not loc:
                    loc = "Remote"
                posted = _clean(r.get("date_posted"))
                desc = _clean(r.get("description"))
                jobs.append({
                    "company_name": company,
                    "company_url": _clean(r.get("company_url")),
                    "title": title,
                    "location_raw": loc,
                    "url": url,
                    "description": (desc[:12000] if desc else None),
                    "posted_at": f"{posted} 00:00:00" if posted and len(posted) == 10 else posted,
                    "source": site,
                })
    return jobs, errors
