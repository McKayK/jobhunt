"""Remote-only job boards with free public APIs: Himalayas, Jobicy, Remotive,
RemoteOK. These surface remote jobs open to US applicants regardless of where
you live — the "remote nationwide" half of the search.

Field shapes were checked against the live APIs (Oct 2026). Notably, the
Himalayas docs say locationRestrictions are objects and pubDate is millis;
the live API returns plain country-name strings and unix seconds. Both forms
are accepted here.

Terms worth respecting (all four ask for it): we keep each board's own job
URL as the posting link and show the board's name as the source. Remotive
asks for at most ~4 fetches a day and RemoteOK for similar restraint, so each
board has a minimum interval between fetches (see MIN_INTERVAL_HOURS).
"""
from __future__ import annotations

import re
from typing import Optional

import requests

from .. import config
from .ats import _iso, _money, strip_html

HEADERS = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}

MIN_INTERVAL_HOURS = {"himalayas": 2, "jobicy": 2, "remotive": 6, "remoteok": 6}

# Location text that means "US applicants welcome".
_US_OK = re.compile(
    r"\b(us|usa|u\.s\.|united states|america|americas|north america|worldwide|"
    r"anywhere|global|world)\b", re.I)


def us_eligible(text: Optional[str]) -> bool:
    """Is a remote board's location restriction open to someone in the US?
    Empty / missing means unrestricted."""
    if not text or not text.strip():
        return True
    return bool(_US_OK.search(text))


def _get(url: str, params: dict) -> requests.Response:
    r = requests.get(url, params=params, headers=HEADERS, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r


def _job(company, title, url, loc, desc, posted, source, salary=None, job_type=None):
    return {
        "company_name": (company or "Unknown company").strip(),
        "title": (title or "").strip(),
        "location_raw": loc,
        "url": url,
        "description": desc,
        "posted_at": posted,
        "salary_text": salary,
        "job_type": job_type,
        "remote_hint": True,
        "source": source,
    }


# --- Himalayas ---------------------------------------------------------------

def fetch_himalayas(terms: list[str], per_term: int = 40) -> list[dict]:
    out = []
    for term in terms:
        for page in (1, 2):
            if len([j for j in out if j.get("_term") == term]) >= per_term:
                break
            data = _get("https://himalayas.app/jobs/api/search",
                        {"q": term, "country": "US", "sort": "recent", "page": page}).json()
            jobs = data.get("jobs") or []
            for j in jobs:
                restr = j.get("locationRestrictions") or []
                names = [r.get("name") if isinstance(r, dict) else str(r) for r in restr]
                if names and not any(re.search(r"united states|^us$|usa", n or "", re.I) for n in names):
                    continue
                loc = "Remote - US" if names else "Remote - Worldwide"
                lo, hi = j.get("minSalary"), j.get("maxSalary")
                sal = None
                if lo or hi:
                    sal = _money(lo, hi, j.get("salaryPeriod"))
                row = _job(j.get("companyName"), j.get("title"),
                           j.get("applicationLink") or j.get("guid"), loc,
                           strip_html(j.get("description")), _iso(j.get("pubDate")),
                           "himalayas", sal, j.get("employmentType"))
                row["_term"] = term
                out.append(row)
            if not data.get("nextCursor") and len(jobs) < 20:
                break
    for r in out:
        r.pop("_term", None)
    return out


# --- Jobicy ------------------------------------------------------------------

def fetch_jobicy(terms: list[str], per_term: int = 50) -> list[dict]:
    out = []
    for term in terms:
        data = _get("https://jobicy.com/api/v2/remote-jobs",
                    {"count": per_term, "geo": "usa", "tag": term}).json()
        for j in data.get("jobs") or []:
            geo = j.get("jobGeo") or ""
            if not us_eligible(geo):
                continue
            sal = _money(j.get("salaryMin"), j.get("salaryMax"), j.get("salaryPeriod"))
            jt = j.get("jobType")
            out.append(_job(j.get("companyName"), j.get("jobTitle"), j.get("url"),
                            f"Remote - {geo}" if geo else "Remote",
                            strip_html(j.get("jobDescription")), _iso(j.get("pubDate")),
                            "jobicy", sal, ", ".join(jt) if isinstance(jt, list) else jt))
    return out


# --- Remotive ----------------------------------------------------------------

def fetch_remotive(terms: list[str], per_term: int = 50) -> list[dict]:
    out = []
    for term in terms:
        data = _get("https://remotive.com/api/remote-jobs",
                    {"search": term, "limit": per_term}).json()
        for j in data.get("jobs") or []:
            loc = j.get("candidate_required_location") or ""
            if not us_eligible(loc):
                continue
            out.append(_job(j.get("company_name"), j.get("title"), j.get("url"),
                            f"Remote - {loc}" if loc else "Remote",
                            strip_html(j.get("description")), _iso(j.get("publication_date")),
                            "remotive", j.get("salary") or None,
                            (j.get("job_type") or "").replace("_", " ") or None))
    return out


# --- RemoteOK ----------------------------------------------------------------

def fetch_remoteok(terms: list[str]) -> list[dict]:
    """RemoteOK returns its whole recent feed in one call; we keep postings
    whose title/tags mention one of your search terms."""
    data = _get("https://remoteok.com/api", {}).json()
    pats = [re.compile(rf"\b{re.escape(t)}\b", re.I) for t in terms if t.strip()]
    out = []
    for j in data[1:] if isinstance(data, list) else []:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        hay = " ".join([j.get("position") or ""] + list(j.get("tags") or []))
        if pats and not any(p.search(hay) for p in pats):
            continue
        loc = j.get("location") or ""
        if not us_eligible(loc) and loc:
            continue
        sal = _money(j.get("salary_min"), j.get("salary_max"), "annual")
        out.append(_job(j.get("company"), j.get("position"), j.get("url"),
                        f"Remote - {loc}" if loc else "Remote",
                        strip_html(j.get("description")), _iso(j.get("epoch") or j.get("date")),
                        "remoteok", sal))
    return out


BOARDS = {
    "himalayas": fetch_himalayas,
    "jobicy": fetch_jobicy,
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
}
