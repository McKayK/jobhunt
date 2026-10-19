"""Fetchers for public ATS (applicant tracking system) job board feeds.

Every fetcher returns list[dict] with the same keys:
    company_name, title, location_raw, url, description, posted_at, source
plus optional: apply_url, salary_text, job_type, remote_hint

No auth needed for any of these. They're the public feeds that power each
company's own careers page.

Supported: Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Workable,
Recruitee, BambooHR, Breezy.
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from .. import config

HEADERS = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section)[^>]*>", re.I)


def strip_html(s: Optional[str], limit: int = 15000) -> Optional[str]:
    """Strip markup to readable text, keeping paragraph breaks.

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
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html_lib.unescape(text)      # entities revealed by tag removal
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit] or None


def _iso(val: Any) -> Optional[str]:
    """Normalize assorted date formats to UTC 'YYYY-MM-DD HH:MM:SS'."""
    if not val:
        return None
    try:
        if isinstance(val, (int, float)):
            ts = val / 1000 if val > 1e11 else val   # millis vs seconds
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        s = str(val).strip().replace("Z", "+00:00")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            s += "T00:00:00+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=kw.pop("headers", HEADERS),
                     timeout=config.HTTP_TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _money(lo: Any, hi: Any, period: Optional[str] = None, cur: str = "$") -> Optional[str]:
    def fmt(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        return f"{cur}{v/1000:.0f}k" if v >= 1000 else f"{cur}{v:,.2f}".rstrip("0").rstrip(".")
    a, b = fmt(lo), fmt(hi)
    if not a and not b:
        return None
    s = f"{a}–{b}" if a and b and a != b else (a or b)
    if period:
        s += {"hourly": "/hr", "hour": "/hr", "yearly": "/yr", "annual": "/yr",
              "year": "/yr", "monthly": "/mo", "month": "/mo"}.get(str(period).lower(), "")
    return s


def _place(*parts: Any) -> Optional[str]:
    vals = [str(p).strip() for p in parts if p and str(p).strip()]
    # Drop consecutive duplicates ("New York, New York" is fine; "UT, UT" isn't).
    out = []
    for v in vals:
        if not out or out[-1].lower() != v.lower():
            out.append(v)
    return ", ".join(out) or None


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
            "posted_at": _iso(j.get("first_published") or j.get("updated_at")),
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
        sal = j.get("salaryRange") or {}
        out.append({
            "company_name": company_name,
            "title": (j.get("text") or "").strip(),
            "location_raw": cats.get("location"),
            "url": j.get("hostedUrl") or j.get("applyUrl"),
            "description": strip_html(j.get("descriptionPlain") or j.get("description")),
            "posted_at": _iso(j.get("createdAt")),
            "job_type": cats.get("commitment"),
            "salary_text": _money(sal.get("min"), sal.get("max"), sal.get("interval", "").replace("per-", "")),
            "remote_hint": (j.get("workplaceType") == "remote"),
            "source": "lever",
        })
    return out


# --- Ashby -----------------------------------------------------------------
# The public posting API returns every listed job with full descriptions and
# structured addresses. (The GraphQL endpoint the old fetcher used started
# returning nothing — Weave, Instructure and Jane showed 0 jobs because of it.)

def fetch_ashby(company_name: str, slug: str) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                f"?includeCompensation=true").json()
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        loc = _place(addr.get("addressLocality"), addr.get("addressRegion")) or j.get("location")
        secondary = [s.get("location") for s in (j.get("secondaryLocations") or []) if s.get("location")]
        if secondary:
            loc = "; ".join([loc] + secondary) if loc else "; ".join(secondary)
        remote = j.get("isRemote") is True or (j.get("workplaceType") or "").lower() == "remote"
        if remote and loc and "remote" not in loc.lower():
            loc = f"Remote - {loc}"
        comp = (j.get("compensation") or {})
        out.append({
            "company_name": company_name,
            "title": (j.get("title") or "").strip(),
            "location_raw": loc,
            "url": j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id')}",
            "apply_url": j.get("applyUrl"),
            "description": (j.get("descriptionPlain") or strip_html(j.get("descriptionHtml"))),
            "posted_at": _iso(j.get("publishedAt")),
            "job_type": j.get("employmentType"),
            "salary_text": comp.get("scrapeableCompensationSalarySummary")
                           or comp.get("compensationTierSummary"),
            "remote_hint": remote,
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
            loc_str = _place(loc.get("city"), loc.get("region"), loc.get("country"))
            if loc.get("remote"):
                loc_str = f"Remote ({loc_str})" if loc_str else "Remote"
            out.append({
                "company_name": company_name,
                "title": (j.get("name") or "").strip(),
                "location_raw": loc_str,
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                "description": None,
                "posted_at": _iso(j.get("releasedDate")),
                "job_type": ((j.get("typeOfEmployment") or {}).get("label")),
                "remote_hint": bool(loc.get("remote")),
                "source": "smartrecruiters",
            })
        offset += len(items)
        if len(items) < 100 or offset >= data.get("totalFound", 0):
            break
    return out


# --- Workday ---------------------------------------------------------------
# Workday's public feed is a POST to a per-tenant CXS endpoint, paginated by
# offset with a total. The site path stored at detection time
# ("/en-US/External") gives the site id.

def _workday_site_id(workday_path: Optional[str]) -> Optional[str]:
    """'/en-US/External' -> 'External'; locale segments are never the site."""
    if not workday_path:
        return None
    parts = [p for p in workday_path.strip("/").split("/") if p]
    parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", p)]
    if not parts:
        return None
    if parts[0].lower() == "job" and len(parts) > 1:
        return None
    return parts[0]


def workday_ids(workday_host: Optional[str], workday_path: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(tenant, site) for both Workday URL styles:
       acme.wd5.myworkdayjobs.com/en-US/External       -> ('acme', 'External')
       wd1.myworkdaysite.com/recruiting/acme/External  -> ('acme', 'External')
    """
    if not workday_host:
        return None, None
    if workday_host.lower().endswith("myworkdaysite.com"):
        parts = [p for p in (workday_path or "").strip("/").split("/") if p]
        parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", p)]
        if len(parts) >= 3 and parts[0].lower() == "recruiting":
            return parts[1], parts[2]
        return None, None
    return workday_host.split(".")[0], _workday_site_id(workday_path)


def fetch_workday(company_name: str, slug: str, workday_host: Optional[str] = None,
                  workday_path: Optional[str] = None) -> list[dict]:
    if not workday_host:
        raise ValueError(f"{company_name}: workday_host is required")
    tenant, site = workday_ids(workday_host, workday_path)
    if not site:
        raise ValueError(
            f"{company_name}: could not derive Workday site id from path "
            f"{workday_path!r} (expected something like '/en-US/External')"
        )

    api = f"https://{workday_host}/wday/cxs/{tenant}/{site}/jobs"
    link_base = (f"https://{workday_host}/recruiting/{tenant}/{site}"
                 if workday_host.endswith("myworkdaysite.com")
                 else f"https://{workday_host}/{site}")
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
            # The site segment matters: /{site}/job/... is the link Workday
            # itself shares; without it some tenants 404.
            url = f"{link_base}{ext}" if ext.startswith("/") else ext
            out.append({
                "company_name": company_name,
                "title": (j.get("title") or "").strip(),
                # locationsText is free text: "Lindon, UT", bare "Lehi", or
                # "5 Locations". geo.py handles the first two.
                "location_raw": (j.get("locationsText") or "").strip() or None,
                "url": url or None,
                "description": None,          # requires a second call per job
                "posted_at": _workday_posted(j.get("postedOn")),
                "source": "workday",
            })

        offset += len(postings)
        total = data.get("total")
        if total is not None:
            if offset >= total:
                break
        elif len(postings) < limit:
            break
        if offset > 5000:                     # runaway guard
            break
    return out


_POSTED_REL = re.compile(r"(\d+)\+?\s*(day|hour|minute|week|month)s?\s*ago", re.I)


def _workday_posted(val: Optional[str]) -> Optional[str]:
    """Workday reports 'Posted 3 Days Ago', not a timestamp."""
    if not val:
        return None
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


# --- Workable --------------------------------------------------------------

def fetch_workable(company_name: str, slug: str) -> list[dict]:
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}").json()
    out = []
    for j in data.get("jobs", []):
        locs = [l for l in (j.get("locations") or []) if not l.get("hidden")]
        if locs:
            loc = "; ".join(filter(None, (_place(l.get("city"), l.get("region"), l.get("country"))
                                          for l in locs)))
        else:
            loc = _place(j.get("city"), j.get("state"), j.get("country"))
        remote = bool(j.get("telecommuting"))
        if remote:
            loc = f"Remote - {loc}" if loc else "Remote"
        out.append({
            "company_name": company_name,
            "title": (j.get("title") or "").strip(),
            "location_raw": loc,
            "url": j.get("url") or j.get("shortlink"),
            "apply_url": j.get("application_url"),
            "description": strip_html(j.get("description")),
            "posted_at": _iso(j.get("published_on") or j.get("created_at")),
            "job_type": j.get("employment_type"),
            "remote_hint": remote,
            "source": "workable",
        })
    return out


# --- Recruitee -------------------------------------------------------------

def fetch_recruitee(company_name: str, slug: str) -> list[dict]:
    data = _get(f"https://{slug}.recruitee.com/api/offers/").json()
    out = []
    for j in data.get("offers", []):
        if j.get("status") and j.get("status") != "published":
            continue
        loc = j.get("location") or _place(j.get("city"), j.get("state_name") or j.get("state_code"),
                                          j.get("country"))
        remote = bool(j.get("remote"))
        if remote and loc and "remote" not in loc.lower():
            loc = f"Remote - {loc}"
        sal = j.get("salary") or {}
        out.append({
            "company_name": company_name,
            "title": (j.get("title") or "").strip(),
            "location_raw": loc or ("Remote" if remote else None),
            "url": j.get("careers_url") or j.get("url"),
            "apply_url": j.get("careers_apply_url"),
            "description": strip_html((j.get("description") or "") + "\n" + (j.get("requirements") or "")),
            "posted_at": _iso(j.get("published_at") or j.get("created_at")),
            "job_type": j.get("employment_type_code"),
            "salary_text": _money(sal.get("min"), sal.get("max"), sal.get("period")) if isinstance(sal, dict) else None,
            "remote_hint": remote,
            "source": "recruitee",
        })
    return out


# --- BambooHR --------------------------------------------------------------
# /careers/list has no dates or descriptions; /careers/{id}/detail has both
# and is fetched lazily (when you open a job or tailor a resume).

def fetch_bamboohr(company_name: str, slug: str) -> list[dict]:
    r = requests.get(f"https://{slug}.bamboohr.com/careers/list",
                     headers=HEADERS, timeout=config.HTTP_TIMEOUT, allow_redirects=False)
    if r.status_code in (301, 302, 303, 307, 308):
        raise ValueError(f"BambooHR board '{slug}' doesn't exist (redirects away)")
    r.raise_for_status()
    data = r.json()
    out = []
    for j in data.get("result", []):
        loc_d = j.get("location") or {}
        ats_d = j.get("atsLocation") or {}
        loc = _place(loc_d.get("city") or ats_d.get("city"),
                     loc_d.get("state") or ats_d.get("state") or ats_d.get("province"),
                     ats_d.get("country"))
        remote = j.get("isRemote") is True
        if remote:
            loc = f"Remote - {loc}" if loc else "Remote"
        out.append({
            "company_name": company_name,
            "title": (j.get("jobOpeningName") or "").strip(),
            "location_raw": loc,
            "url": f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
            "description": None,
            "posted_at": None,
            "job_type": j.get("employmentStatusLabel"),
            "remote_hint": remote,
            "source": "bamboohr",
        })
    return out


# --- Breezy HR -------------------------------------------------------------

def fetch_breezy(company_name: str, slug: str) -> list[dict]:
    data = _get(f"https://{slug}.breezy.hr/json").json()
    out = []
    for j in data if isinstance(data, list) else []:
        loc_d = j.get("location") or {}
        state = loc_d.get("state")
        state = state.get("name") if isinstance(state, dict) else state
        country = loc_d.get("country")
        country = country.get("name") if isinstance(country, dict) else country
        loc = loc_d.get("name") or _place(loc_d.get("city"), state, country)
        remote = bool(loc_d.get("is_remote"))
        if remote and loc and "remote" not in loc.lower():
            loc = f"Remote - {loc}"
        typ = j.get("type")
        out.append({
            "company_name": company_name,
            "title": (j.get("name") or "").strip(),
            "location_raw": loc or ("Remote" if remote else None),
            "url": j.get("url") or f"https://{slug}.breezy.hr/p/{j.get('friendly_id')}",
            "description": None,
            "posted_at": _iso(j.get("published_date")),
            "job_type": typ.get("name") if isinstance(typ, dict) else typ,
            "salary_text": j.get("salary") if isinstance(j.get("salary"), str) else None,
            "remote_hint": remote,
            "source": "breezy",
        })
    return out


# --- Registry --------------------------------------------------------------

FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "bamboohr": fetch_bamboohr,
    "breezy": fetch_breezy,
}
SUPPORTED = tuple(FETCHERS)


def _row_get(row: Any, key: str):
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def fetch_company(company_name: str, ats: str, slug: str, row: Any = None) -> list[dict]:
    """Fetch one company's jobs. `row` is the optional companies table row
    (Workday needs workday_host and workday_path from it)."""
    fn = FETCHERS.get(ats)
    if not fn:
        raise ValueError(f"No fetcher for ATS '{ats}'")
    if ats == "workday":
        jobs = fn(company_name, slug, _row_get(row, "workday_host"), _row_get(row, "workday_path"))
    else:
        jobs = fn(company_name, slug)
    return [j for j in jobs if j.get("title") and j.get("url")]


def board_url(ats: str, slug: Optional[str], workday_host: Optional[str] = None,
              workday_path: Optional[str] = None) -> Optional[str]:
    """Public, human-friendly link to a company's job board."""
    if not slug and ats != "workday":
        return None
    return {
        "greenhouse": f"https://job-boards.greenhouse.io/{slug}",
        "lever": f"https://jobs.lever.co/{slug}",
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
        "smartrecruiters": f"https://jobs.smartrecruiters.com/{slug}",
        "workable": f"https://apply.workable.com/{slug}/",
        "recruitee": f"https://{slug}.recruitee.com/",
        "bamboohr": f"https://{slug}.bamboohr.com/careers",
        "breezy": f"https://{slug}.breezy.hr/",
        "workday": (f"https://{workday_host}{(workday_path or '').split('/job/')[0]}" if workday_host else None),
    }.get(ats)


# --- Lazy full descriptions ---------------------------------------------------
# Several boards list jobs without descriptions. When you open a job or tailor
# a resume, we fetch the one description we need.

def fetch_description(job: Any, company: Any = None) -> Optional[str]:
    src = _row_get(job, "source")
    url = _row_get(job, "url") or ""
    try:
        if src == "workday":
            p = urlparse(url)
            host = p.netloc
            segs = [s for s in p.path.split("/") if s]
            if "job" in segs:
                i = segs.index("job")
                ext = "/" + "/".join(segs[i:])
                tenant, site = workday_ids(host, "/" + "/".join(segs[:i]))
                if not site:   # old-style stored URL without the site segment
                    tenant, site = workday_ids(_row_get(company, "workday_host") or host,
                                               _row_get(company, "workday_path"))
                if tenant and site:
                    d = _get(f"https://{host}/wday/cxs/{tenant}/{site}{ext}").json()
                    return strip_html((d.get("jobPostingInfo") or {}).get("jobDescription"))
        elif src == "smartrecruiters":
            m = re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", url)
            if m:
                d = _get(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}").json()
                secs = ((d.get("jobAd") or {}).get("sections") or {})
                parts = [(secs.get(k) or {}).get("text") for k in
                         ("companyDescription", "jobDescription", "qualifications", "additionalInformation")]
                return strip_html("\n".join(p for p in parts if p))
        elif src == "bamboohr":
            m = re.search(r"https://([^.]+)\.bamboohr\.com/careers/(\d+)", url)
            if m:
                d = _get(f"https://{m.group(1)}.bamboohr.com/careers/{m.group(2)}/detail").json()
                jo = ((d.get("result") or {}).get("jobOpening") or {})
                return strip_html(jo.get("description"))
        elif src == "breezy":
            d = _get(url.rstrip("/") + "/json" if "/p/" in url else url).json()
            return strip_html(d.get("description"))
    except Exception:
        pass
    # Generic fallback: the posting page itself (works for most server-rendered
    # pages; JS-only pages yield little and we just return None).
    if url and not re.search(r"indeed\.com|linkedin\.com|glassdoor\.com|ziprecruiter\.com", url):
        try:
            r = requests.get(url, headers={"User-Agent": config.BROWSER_UA},
                             timeout=config.HTTP_TIMEOUT)
            if r.ok and "html" in r.headers.get("content-type", ""):
                body = re.search(r"<main[^>]*>(.*?)</main>", r.text, re.S | re.I)
                text = strip_html(body.group(1) if body else r.text)
                if text and len(text) > 400:
                    return text
        except Exception:
            pass
    return None
