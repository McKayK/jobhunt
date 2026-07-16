"""Fetchers for public ATS job board APIs.

Every fetcher returns list[dict] with the same keys:
    company_name, title, location_raw, url, description, posted_at, source

No auth needed for any of these. They're the public feeds that power each
company's own careers page.
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .. import config

HEADERS = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
_TAG = re.compile(r"<[^>]+>")


def strip_html(s: Optional[str], limit: int = 12000) -> Optional[str]:
    """Strip markup to readable text.

    Greenhouse returns *entity-encoded* HTML (&lt;p&gt;...), so unescape first,
    then strip tags. Loop because some sources double-encode.
    """
    if not s:
        return None
    text = s
    for _ in range(3):
        unescaped = html_lib.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = _TAG.sub(" ", text)
    text = html_lib.unescape(text)      # entities revealed by tag removal
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def _iso(val: Any) -> Optional[str]:
    """Normalize assorted date formats to UTC 'YYYY-MM-DD HH:MM:SS'.

    Sources mix epoch millis, Z-suffixed UTC, and offset-aware strings. Storing
    them unconverted would make 'newest first' subtly wrong across companies.
    """
    if not val:
        return None
    try:
        if isinstance(val, (int, float)):
            ts = val / 1000 if val > 1e11 else val   # Lever/Ashby use millis
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        s = str(val).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=config.HTTP_TIMEOUT, **kw)
    r.raise_for_status()
    return r


# --- Greenhouse ------------------------------------------------------------

def fetch_greenhouse(company_name: str, slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = _get(url).json()
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name")
        out.append({
            "company_name": company_name,
            "title": j.get("title", "").strip(),
            "location_raw": loc,
            "url": j.get("absolute_url"),
            "description": strip_html(j.get("content")),
            "posted_at": _iso(j.get("updated_at") or j.get("first_published")),
            "source": "greenhouse",
        })
    return out


# --- Lever -----------------------------------------------------------------

def fetch_lever(company_name: str, slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = _get(url).json()
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "company_name": company_name,
            "title": (j.get("text") or "").strip(),
            "location_raw": cats.get("location"),
            "url": j.get("hostedUrl") or j.get("applyUrl"),
            "description": strip_html(j.get("descriptionPlain") or j.get("description")),
            "posted_at": _iso(j.get("createdAt")),
            "source": "lever",
        })
    return out


# --- Ashby -----------------------------------------------------------------

# Ashby's schema is picky; keep the query minimal and resilient.
_ASHBY_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    jobPostings {
      id
      title
      locationName
      employmentType
      publishedAt
    }
  }
}
"""


def fetch_ashby(company_name: str, slug: str) -> list[dict]:
    r = requests.post(
        "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={
            "operationName": "ApiJobBoardWithTeams",
            "variables": {"organizationHostedJobsPageName": slug},
            "query": _ASHBY_QUERY,
        },
        timeout=config.HTTP_TIMEOUT,
    )
    r.raise_for_status()
    board = (r.json().get("data") or {}).get("jobBoard") or {}
    out = []
    for j in board.get("jobPostings", []):
        out.append({
            "company_name": company_name,
            "title": (j.get("title") or "").strip(),
            "location_raw": j.get("locationName"),
            "url": f"https://jobs.ashbyhq.com/{slug}/{j.get('id')}",
            "description": None,   # detail requires a second call; fetch lazily
            "posted_at": _iso(j.get("publishedAt")),
            "source": "ashby",
        })
    return out


# --- SmartRecruiters -------------------------------------------------------

def fetch_smartrecruiters(company_name: str, slug: str) -> list[dict]:
    out, offset = [], 0
    while True:
        url = (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
               f"?limit=100&offset={offset}")
        data = _get(url).json()
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            parts = [loc.get("city"), loc.get("region"), loc.get("country")]
            loc_str = ", ".join(p for p in parts if p)
            if loc.get("remote"):
                loc_str = f"Remote ({loc_str})" if loc_str else "Remote"
            out.append({
                "company_name": company_name,
                "title": (j.get("name") or "").strip(),
                "location_raw": loc_str or None,
                "url": (j.get("applyUrl")
                        or f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}"),
                "description": None,
                "posted_at": _iso(j.get("releasedDate")),
                "source": "smartrecruiters",
            })
        offset += len(items)
        if len(items) < 100 or offset >= data.get("totalFound", 0):
            break
    return out


# --- Workday ---------------------------------------------------------------
# Workday is the odd one out. Its public feed is a POST to a per-tenant CXS
# endpoint, not a GET, and the response is paginated by offset with a total.
# The site path stored at detection time ("/en-US/External") gives us the
# site id we need; everything before it is boilerplate.

def _workday_site_id(workday_path: Optional[str]) -> Optional[str]:
    """Pull the site identifier out of a stored Workday path.

    '/en-US/External'      -> 'External'
    '/External/job/Foo'    -> 'External'
    Locale segments like 'en-US' are never the site id.
    """
    if not workday_path:
        return None
    parts = [p for p in workday_path.strip("/").split("/") if p]
    parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", p)]
    if not parts:
        return None
    if parts[0].lower() == "job" and len(parts) > 1:
        return None
    return parts[0]


def fetch_workday(company_name: str, slug: str, workday_host: Optional[str] = None,
                  workday_path: Optional[str] = None) -> list[dict]:
    if not workday_host:
        raise ValueError(f"{company_name}: workday_host is required")
    site = _workday_site_id(workday_path)
    if not site:
        raise ValueError(
            f"{company_name}: could not derive Workday site id from path "
            f"{workday_path!r} (expected something like '/en-US/External')"
        )

    tenant = workday_host.split(".")[0]
    api = f"https://{workday_host}/wday/cxs/{tenant}/{site}/jobs"
    out: list[dict] = []
    offset, limit = 0, 20      # Workday caps its own page size at 20.

    while True:
        r = requests.post(
            api,
            json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=config.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break

        for j in postings:
            ext = j.get("externalPath") or ""
            url = f"https://{workday_host}{ext}" if ext.startswith("/") else ext
            out.append({
                "company_name": company_name,
                "title": (j.get("title") or "").strip(),
                # locationsText is free text: "Lindon, UT" but also
                # "5 Locations" when a req spans sites. geo.py treats an
                # unparseable string as 'unknown' rather than guessing.
                "location_raw": (j.get("locationsText") or "").strip() or None,
                "url": url or None,
                "description": None,          # requires a second call per job
                "posted_at": _workday_posted(j.get("postedOn")),
                "source": "workday",
            })

        offset += len(postings)
        total = data.get("total")
        if total is not None:
            # Trust `total` over page size: Workday can return a short page
            # mid-run, and breaking on that silently truncates the board.
            if offset >= total:
                break
        elif len(postings) < limit:
            break
        if offset > 5000:                     # runaway guard
            break
    return out


_POSTED_REL = re.compile(r"(\d+)\+?\s*(day|hour|minute|week|month)s?\s*ago", re.I)


def _workday_posted(val: Optional[str]) -> Optional[str]:
    """Workday reports 'Posted 3 Days Ago', not a timestamp.

    Convert to an approximate UTC datetime so cross-company sorting works.
    'Posted Today' / 'Posted Yesterday' are special-cased.
    """
    if not val:
        return None
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    low = val.lower()
    if "today" in low:
        return now.strftime("%Y-%m-%d %H:%M:%S")
    if "yesterday" in low:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    m = _POSTED_REL.search(low)
    if not m:
        return _iso(val)
    n, unit = int(m.group(1)), m.group(2)
    delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n),
             "day": timedelta(days=n), "week": timedelta(weeks=n),
             "month": timedelta(days=30 * n)}[unit]
    return (now - delta).strftime("%Y-%m-%d %H:%M:%S")


# --- Registry --------------------------------------------------------------

FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def fetch_company(company_name: str, ats: str, slug: str, row: Any = None) -> list[dict]:
    """Fetch one company's jobs.

    `row` is the optional companies table row. Workday needs workday_host and
    workday_path from it; the other ATSes are fully identified by slug alone,
    so existing three-arg callers keep working.
    """
    fn = FETCHERS.get(ats)
    if not fn:
        raise ValueError(f"No fetcher for ATS '{ats}'")

    if ats == "workday":
        host = path = None
        if row is not None:
            try:
                host, path = row["workday_host"], row["workday_path"]
            except (KeyError, IndexError, TypeError):
                host = path = None
        jobs = fn(company_name, slug, host, path)
    else:
        jobs = fn(company_name, slug)
    return [j for j in jobs if j.get("title") and j.get("url")]
