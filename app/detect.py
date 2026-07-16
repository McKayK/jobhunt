"""Detect which ATS a company uses and extract its board slug.

Strategy, cheapest first:
  1. The URL you gave me might already BE an ATS URL -> parse it directly.
  2. Fetch the careers page and regex the raw HTML for ATS URLs. Most careers
     pages either redirect to the ATS or embed it in an iframe/script/link.
  3. Probe the obvious slug guesses against each ATS API and see what answers.

Step 3 is what saves you the manual work: for most companies the slug is just
the company name lowercased with punctuation stripped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests

from . import config

HEADERS = {"User-Agent": config.USER_AGENT, "Accept": "text/html,application/json,*/*"}


@dataclass
class Detection:
    ats: str
    slug: Optional[str] = None
    workday_host: Optional[str] = None
    workday_path: Optional[str] = None
    confidence: str = "low"      # high | medium | low
    note: str = ""


# --- URL patterns ----------------------------------------------------------

PATTERNS = [
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_\-]+)", re.I)),
    ("greenhouse", re.compile(r"job-boards\.greenhouse\.io/([a-z0-9_\-]+)", re.I)),
    ("greenhouse", re.compile(r"api\.greenhouse\.io/v1/boards/([a-z0-9_\-]+)", re.I)),
    ("lever",      re.compile(r"jobs\.lever\.co/([a-z0-9_\-]+)", re.I)),
    ("lever",      re.compile(r"api\.lever\.co/v0/postings/([a-z0-9_\-]+)", re.I)),
    ("ashby",      re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_\-\.]+)", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([a-z0-9_\-]+)", re.I)),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/([a-z0-9_\-]+)", re.I)),
    ("recruitee",  re.compile(r"([a-z0-9\-]+)\.recruitee\.com", re.I)),
    ("workable",   re.compile(r"apply\.workable\.com/([a-z0-9_\-]+)", re.I)),
]

WORKDAY_PAT = re.compile(
    r"(https?://)?([a-z0-9\-]+\.(?:wd\d+)\.myworkdayjobs\.com)(/[A-Za-z0-9_\-/]+)?", re.I
)

# Slugs that show up on nearly every page and are never the company's own board.
SLUG_BLOCKLIST = {
    "embed", "job_board", "jobs", "careers", "www", "api", "static", "assets",
    "images", "css", "js", "v1", "v0", "boards", "search", "company",
}


def _clean_slug(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    s = slug.strip().strip("/").lower()
    if s in SLUG_BLOCKLIST or len(s) < 2:
        return None
    return s


def detect_from_url(url: str) -> Optional[Detection]:
    """Parse an ATS out of a URL string without any network calls."""
    if not url:
        return None

    m = WORKDAY_PAT.search(url)
    if m:
        host = m.group(2)
        path = (m.group(3) or "").rstrip("/")
        tenant = host.split(".")[0]
        return Detection("workday", tenant, host, path or None, "high",
                         "Workday needs a tenant + site path; verify the path.")

    for ats, pat in PATTERNS:
        m = pat.search(url)
        if m:
            slug = _clean_slug(m.group(1))
            if slug:
                return Detection(ats, slug, confidence="high")
    return None


def detect_from_html(html: str) -> Optional[Detection]:
    """Scan raw page source for embedded ATS references."""
    if not html:
        return None

    m = WORKDAY_PAT.search(html)
    if m:
        host = m.group(2)
        path = (m.group(3) or "").rstrip("/")
        return Detection("workday", host.split(".")[0], host, path or None, "medium",
                         "Found Workday in page source.")

    # Count hits so a stray link doesn't beat the real board.
    best: Optional[Detection] = None
    best_count = 0
    for ats, pat in PATTERNS:
        for m in pat.finditer(html):
            slug = _clean_slug(m.group(1))
            if not slug:
                continue
            count = len(pat.findall(html))
            if count > best_count:
                best, best_count = Detection(ats, slug, confidence="medium",
                                             note="Found in careers page source."), count
    return best


# --- Live probes -----------------------------------------------------------

def _get(url: str, **kw):
    return requests.get(url, headers=HEADERS, timeout=config.HTTP_TIMEOUT, **kw)


def probe_greenhouse(slug: str) -> bool:
    try:
        r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        return r.status_code == 200 and isinstance(r.json().get("jobs"), list)
    except Exception:
        return False


def probe_lever(slug: str) -> bool:
    """Lever 404s on unknown slugs, but a valid-but-empty board also returns [].
    Require at least one posting so we don't accept a guessed slug that merely
    failed to error."""
    try:
        r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if r.status_code != 200:
            return False
        data = r.json()
        return isinstance(data, list) and len(data) > 0
    except Exception:
        return False


def probe_ashby(slug: str) -> bool:
    try:
        r = requests.post(
            "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
            headers={**HEADERS, "Content-Type": "application/json"},
            json={
                "operationName": "ApiJobBoardWithTeams",
                "variables": {"organizationHostedJobsPageName": slug},
                "query": "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) "
                         "{ jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: "
                         "$organizationHostedJobsPageName) { jobPostings { id title } } }",
            },
            timeout=config.HTTP_TIMEOUT,
        )
        data = r.json()
        return bool((data.get("data") or {}).get("jobBoard"))
    except Exception:
        return False


def probe_smartrecruiters(slug: str) -> bool:
    """SmartRecruiters returns HTTP 200 with an empty result set for company
    slugs that don't exist, so status alone proves nothing. Require a non-empty
    postings list — this is what made ~50 fabricated slugs look verified."""
    try:
        r = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        if data.get("totalFound", 0) > 0:
            return True
        return bool(data.get("content"))
    except Exception:
        return False


def probe_workday(slug: str, workday_host: str | None = None,
                  workday_path: str | None = None) -> bool:
    """Verify a Workday tenant+site actually serves postings.

    Unlike the other probes this needs host and path, not just a slug -- the
    tenant alone doesn't identify a board. Requires a non-empty jobPostings
    list: a wrong site id still returns HTTP 200 with an empty set, which is
    exactly the false-positive that fabricated ~50 slugs last time.
    """
    if not workday_host:
        return False
    try:
        from .fetchers.ats import _workday_site_id
        site = _workday_site_id(workday_path)
        if not site:
            return False
        tenant = workday_host.split(".")[0]
        r = requests.post(
            f"https://{workday_host}/wday/cxs/{tenant}/{site}/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={"User-Agent": config.USER_AGENT, "Content-Type": "application/json"},
            timeout=config.HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        if data.get("total", 0) > 0:
            return True
        return bool(data.get("jobPostings"))
    except Exception:
        return False


PROBES = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "ashby": probe_ashby,
    "smartrecruiters": probe_smartrecruiters,
    # NOTE: probe_workday needs (slug, workday_host, workday_path). Callers
    # iterating PROBES with a bare slug must special-case it -- see cli.py.
    "workday": probe_workday,
}


def probe_row(row) -> Optional[bool]:
    """Probe a companies-table row, handling each ATS's argument shape.

    Returns None when the ATS has no probe (caller should skip, not treat as
    a failure).
    """
    fn = PROBES.get(row["ats"])
    if not fn:
        return None
    if row["ats"] == "workday":
        try:
            host, path = row["workday_host"], row["workday_path"]
        except (KeyError, IndexError, TypeError):
            return False
        return fn(row["slug"], host, path)
    return fn(row["slug"])


def slug_guesses(company_name: str, careers_url: str | None = None) -> list[str]:
    """Generate plausible slugs, best guess first."""
    name = company_name.strip().lower()
    base = re.sub(r"[^a-z0-9\s\-]", "", name)
    compact = re.sub(r"[\s\-]+", "", base)
    hyphen = re.sub(r"[\s]+", "-", base.strip())

    guesses = [compact, hyphen]

    # Drop corporate suffixes: "Acme Software Inc" -> "acme"
    stripped = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|"
                      r"technology|labs|software|systems|group|holdings)\b", "", base)
    stripped_compact = re.sub(r"[\s\-]+", "", stripped)
    if stripped_compact and stripped_compact not in guesses:
        guesses.append(stripped_compact)

    # The registrable domain is often the slug.
    if careers_url:
        host = urlparse(careers_url if "//" in careers_url else f"https://{careers_url}").netloc
        host = host.replace("www.", "").split(":")[0]
        if host:
            domain_root = host.split(".")[0]
            if domain_root and domain_root not in guesses and domain_root not in SLUG_BLOCKLIST:
                guesses.insert(0, domain_root)

    seen, out = set(), []
    for g in guesses:
        g = _clean_slug(g)
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _norm_company(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|"
               r"technology|labs|software|systems|group|holdings|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def detect(company_name: str, careers_url: str | None = None,
           probe: bool = True) -> Detection:
    """Full detection pipeline for one company.

    Order matters: every step before the last is *evidence*. The last is a
    guess, and a guess is only accepted if a live board confirms it.

    Known limit: this only sees raw HTML. Most careers pages render their job
    list with JavaScript, so the ATS link often isn't in the source and we fall
    through to guessing. SmartRecruiters identifiers in particular are
    case-sensitive and frequently unrelated to the brand name
    ('TheNielsenCompany', not 'Nielsen'), so guessing cannot find them.
    Use `python -m app.discover` for those — it renders the page in a real
    browser and reads the identifier out of the network traffic.
    """
    # 1. The URL itself already names the ATS.
    if careers_url:
        d = detect_from_url(careers_url)
        if d:
            return d

    # 2. The careers page: redirect target, then page source.
    if careers_url:
        try:
            url = careers_url if "//" in careers_url else f"https://{careers_url}"
            r = _get(url, allow_redirects=True)
            d = detect_from_url(r.url)
            if d:
                d.note = "Careers page redirects to ATS."
                return d
            d = detect_from_html(r.text)
            if d:
                return d
        except Exception:
            pass

    # 3. Last resort: guess slugs, and require a live board to confirm.
    if probe:
        for slug in slug_guesses(company_name, careers_url):
            for ats, fn in PROBES.items():
                if fn(slug):
                    return Detection(ats, slug, confidence="medium",
                                     note=f"Guessed slug confirmed against live board: {ats}/{slug}")

    return Detection("unknown", None, confidence="low",
                     note="No ATS in raw HTML. Likely JS-rendered — try: python -m app.discover")