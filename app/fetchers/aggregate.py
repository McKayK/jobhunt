"""Job sites: Indeed, ZipRecruiter, Google Jobs, LinkedIn, Glassdoor (python-jobspy).

This finds jobs at *any* company near your ZIP, not just the ones you track,
and — with remote_nationwide on — remote jobs open to anyone in the US.

What JobSpy 1.4 adds that matters here:
- `job_url_direct`: for external-apply postings, Indeed hands back the
  employer's own apply link (very often a Greenhouse/Lever/Workday URL). We
  store it as `apply_url`, link to it first in the UI, and use it to start
  tracking that company's board automatically (see autodiscover.py).
- `company_url_direct`, rating/review counts, posted emails: used by the scam
  filter (a "company" with no website, no reviews, and a gmail contact is a
  bad sign).
- No more numpy==1.26.3 pin, so it installs normally on Python 3.13.

Notes:
- Each site is scraped independently; failures are per-site.
- One search per include-keyword. With no keywords, local search runs one
  blank search; nationwide remote search is skipped (it'd be every remote job
  in America).
- LinkedIn/Glassdoor are opt-in: they throttle scrapers sooner, and mostly
  duplicate Indeed. Worth a try if Indeed alone feels thin.
"""
from __future__ import annotations

import math
import re
from typing import Optional

SITE_SETTING_MAP = {
    "indeed": "use_indeed",
    "zip_recruiter": "use_zip_recruiter",
    "google": "use_google",
    "linkedin": "use_linkedin",
    "glassdoor": "use_glassdoor",
}
# Sites that support a nationwide "remote only" search well.
REMOTE_CAPABLE = ("indeed", "linkedin", "zip_recruiter", "glassdoor")


def _clean(val) -> Optional[str]:
    """pandas gives us NaN for missing values; normalize to None."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    s = str(val).strip()
    return s or None


def _num(val) -> Optional[float]:
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _salary(r: dict) -> Optional[str]:
    lo, hi = _num(r.get("min_amount")), _num(r.get("max_amount"))
    if not lo and not hi:
        return None
    interval = (_clean(r.get("interval")) or "").lower()
    suffix = {"hourly": "/hr", "yearly": "/yr", "monthly": "/mo",
              "weekly": "/wk", "daily": "/day"}.get(interval, "")

    def f(v):
        if v is None:
            return None
        return f"${v/1000:.0f}k" if v >= 1000 else f"${v:,.2f}".rstrip("0").rstrip(".")
    a, b = f(lo), f(hi)
    return (f"{a}–{b}" if a and b and a != b else (a or b)) + suffix


def _row_to_job(r: dict, site: str, force_remote: bool = False) -> Optional[dict]:
    title = _clean(r.get("title"))
    url = _clean(r.get("job_url"))
    company = _clean(r.get("company")) or "Unknown company"
    if not title or not url:
        return None
    loc = _clean(r.get("location"))
    is_remote = bool(r.get("is_remote")) if r.get("is_remote") is not None else False
    is_remote = is_remote or force_remote
    if is_remote:
        if not loc:
            loc = "Remote"
        elif "remote" not in loc.lower():
            loc = f"Remote - {loc}"
    posted = _clean(r.get("date_posted"))
    desc = _clean(r.get("description"))
    direct = _clean(r.get("job_url_direct"))
    emails = _clean(r.get("emails"))
    return {
        "company_name": company,
        "company_url": _clean(r.get("company_url_direct")),
        "title": title,
        "location_raw": loc,
        "url": url,
        "apply_url": direct,
        "description": (desc[:15000] if desc else None),
        "posted_at": f"{posted} 00:00:00" if posted and len(posted) == 10 else posted,
        "salary_text": _salary(r),
        "job_type": _clean(r.get("job_type")),
        "remote_hint": is_remote,
        "source": site,
        # Signals for the scam filter (not stored as columns).
        "_emails": [e.strip() for e in emails.split(",")] if emails else [],
        "_company_rating": _num(r.get("company_rating")),
        "_company_reviews": _num(r.get("company_reviews_count")),
        "_company_profile": bool(_clean(r.get("company_url"))),
        "_listing_type": _clean(r.get("listing_type")),
    }


def _scrape(site: str, **kwargs):
    from jobspy import scrape_jobs
    return scrape_jobs(site_name=[site], country_indeed="USA",
                       description_format="markdown", verbose=0, **kwargs)


def fetch_aggregate(sites: list[str], home_zip: str, radius_miles: float,
                    search_terms: list[str], results_wanted: int = 100,
                    hours_old: int = 0, on_progress=None,
                    place_name: Optional[str] = None,
                    remote_nationwide: bool = False) -> tuple[list[dict], list[str]]:
    """Search the given sites around home_zip (and optionally US-wide remote).
    Returns (jobs, errors)."""
    try:
        import jobspy  # noqa: F401
    except ImportError:
        return [], ["python-jobspy is not installed — run: pip install -r requirements.txt"]

    terms = [t for t in (search_terms or []) if t.strip()]
    local_terms = terms or [""]
    jobs: list[dict] = []
    errors: list[str] = []
    where = place_name or home_zip

    plan: list[tuple[str, str, bool]] = [(s, t, False) for s in sites for t in local_terms]
    if remote_nationwide and terms:
        plan += [(s, t, True) for s in sites if s in REMOTE_CAPABLE for t in terms]

    for site, term, remote in plan:
        label = f"{site}{' remote' if remote else ''}" + (f" '{term}'" if term else "")
        if on_progress:
            on_progress(f"Searching {label}…")
        try:
            kwargs = dict(results_wanted=results_wanted)
            if hours_old and hours_old > 0:
                kwargs["hours_old"] = hours_old
            if term:
                kwargs["search_term"] = term
            if remote:
                kwargs.update(location="United States", is_remote=True)
                kwargs["google_search_term"] = f"remote {term} jobs in the United States"
            else:
                # Indeed geocodes a ZIP precisely; the others want a place name.
                kwargs["location"] = home_zip if site == "indeed" else where
                kwargs["distance"] = int(round(radius_miles))
                kwargs["google_search_term"] = (
                    f"{term + ' ' if term else ''}jobs near {where} within {int(radius_miles)} miles")
            df = _scrape(site, **kwargs)
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {str(e)[:120]}")
            continue

        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            j = _row_to_job(row.to_dict(), site, force_remote=remote)
            if j:
                jobs.append(j)
    return jobs, errors


def plan_size(sites: list[str], search_terms: list[str], remote_nationwide: bool) -> int:
    terms = [t for t in (search_terms or []) if t.strip()]
    n = len(sites) * max(1, len(terms))
    if remote_nationwide and terms:
        n += len([s for s in sites if s in REMOTE_CAPABLE]) * len(terms)
    return n
