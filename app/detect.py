"""Detect which ATS a company uses and extract its board identifier.

Strategy, cheapest first:
  1. The URL you gave might already BE an ATS URL -> parse it directly.
     (This is also how job-site postings get promoted: Indeed's direct apply
     link usually points straight at the employer's ATS.)
  2. Fetch the careers page and regex the raw HTML for ATS URLs. Most careers
     pages either redirect to the ATS or embed it in an iframe/script/link.
  3. Probe the obvious slug guesses against each ATS API and see what answers.

Every probe requires a NON-EMPTY live board. Several ATSes answer 200 with an
empty list for boards that don't exist (SmartRecruiters, Workday with a wrong
site id) — accepting those once fabricated ~50 companies.

Recognized-but-unreadable systems (iCIMS, Paylocity, UKG, Rippling, Jobvite,
ADP, Taleo, ...) come back as ats='link': we can't pull their jobs
automatically, but we can still hand you the link to their job board.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from . import config
from .fetchers.ats import SUPPORTED, workday_ids

HEADERS = {"User-Agent": config.USER_AGENT, "Accept": "text/html,application/json,*/*"}


@dataclass
class Detection:
    ats: str
    slug: Optional[str] = None
    workday_host: Optional[str] = None
    workday_path: Optional[str] = None
    confidence: str = "low"      # high | medium | low
    note: str = ""
    url: Optional[str] = None    # the board / careers link we found

    @property
    def supported(self) -> bool:
        return self.ats in SUPPORTED and bool(self.slug or self.workday_host)


# --- URL patterns ----------------------------------------------------------

_S = r"([a-z0-9][a-z0-9_\-\.]*)"
PATTERNS = [
    ("greenhouse", re.compile(r"boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?" + _S, re.I)),
    ("greenhouse", re.compile(r"job-boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?" + _S, re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + _S, re.I)),
    ("greenhouse", re.compile(r"api\.greenhouse\.io/v1/boards/" + _S, re.I)),
    ("lever",      re.compile(r"jobs\.(?:eu\.)?lever\.co/" + _S, re.I)),
    ("lever",      re.compile(r"api\.lever\.co/v0/postings/" + _S, re.I)),
    ("ashby",      re.compile(r"jobs\.ashbyhq\.com/" + _S, re.I)),
    ("ashby",      re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _S, re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/" + _S, re.I)),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/" + _S, re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/" + _S, re.I)),
    ("workable",   re.compile(r"apply\.workable\.com/(?:api/v\d/(?:widget/)?accounts/)?" + _S, re.I)),
    ("workable",   re.compile(r"\b([a-z0-9\-]+)\.workable\.com", re.I)),
    ("recruitee",  re.compile(r"\b([a-z0-9\-]+)\.recruitee\.com", re.I)),
    ("bamboohr",   re.compile(r"\b([a-z0-9\-]+)\.bamboohr\.com", re.I)),
    ("breezy",     re.compile(r"\b([a-z0-9\-]+)\.breezy\.hr", re.I)),
]

# Systems we recognize but can't read. Value: a readable name for the UI.
LINK_ONLY = [
    (re.compile(r"[a-z0-9\-]+\.icims\.com", re.I), "iCIMS"),
    (re.compile(r"recruiting\.paylocity\.com", re.I), "Paylocity"),
    (re.compile(r"recruiting2?\.ultipro\.com|ukg\.net|ukgpro\.com", re.I), "UKG"),
    (re.compile(r"ats\.rippling\.com", re.I), "Rippling"),
    (re.compile(r"jobs\.jobvite\.com|careers\.jobvite\.com", re.I), "Jobvite"),
    (re.compile(r"applytojob\.com", re.I), "JazzHR"),
    (re.compile(r"workforcenow\.adp\.com|myjobs\.adp\.com|recruiting\.adp\.com", re.I), "ADP"),
    (re.compile(r"taleo\.net", re.I), "Taleo"),
    (re.compile(r"successfactors\.(?:com|eu)|jobs\.sap\.com", re.I), "SuccessFactors"),
    (re.compile(r"oraclecloud\.com/hcmUI", re.I), "Oracle"),
    (re.compile(r"[a-z0-9\-]+\.teamtailor\.com", re.I), "Teamtailor"),
    (re.compile(r"[a-z0-9\-]+\.pinpointhq\.com", re.I), "Pinpoint"),
    (re.compile(r"jobs\.gem\.com", re.I), "Gem"),
    (re.compile(r"app\.dover\.com", re.I), "Dover"),
    (re.compile(r"[a-z0-9\-]+\.jobs\.personio\.(?:de|com)", re.I), "Personio"),
    (re.compile(r"paycomonline\.net", re.I), "Paycom"),
    (re.compile(r"recruitingbypaycor\.com|paycor\.com", re.I), "Paycor"),
    (re.compile(r"[a-z0-9\-]+\.hirebridge\.com|hirebridge\.com", re.I), "Hirebridge"),
    (re.compile(r"careers-page\.com|clearcompany\.com", re.I), "ClearCompany"),
    (re.compile(r"[a-z0-9\-]+\.hire\.trakstar\.com", re.I), "Trakstar"),
]

WORKDAY_PAT = re.compile(
    r"(https?://)?([a-z0-9\-]+\.(?:wd\d+)\.myworkdayjobs\.com)(/[A-Za-z0-9_\-/]+)?", re.I
)
WORKDAY_SITE_PAT = re.compile(
    r"(https?://)?((?:wd\d+\.)?myworkdaysite\.com)(/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+)", re.I
)

# Slugs that show up on nearly every page and are never the company's own board.
SLUG_BLOCKLIST = {
    "embed", "job_board", "jobs", "careers", "www", "api", "static", "assets",
    "images", "css", "js", "v1", "v0", "boards", "search", "company", "app",
    "apply", "help", "support", "blog", "status", "docs", "j", "widget",
    "accounts", "public", "careers-page", "cdn", "login", "signup",
}


def _clean_slug(slug: Optional[str], ats: str = "") -> Optional[str]:
    if not slug:
        return None
    s = slug.strip().strip("/").rstrip(".")
    if ats != "smartrecruiters":       # SmartRecruiters ids are case-sensitive
        s = s.lower()
    if s.lower() in SLUG_BLOCKLIST or len(s) < 2:
        return None
    return s


def detect_from_url(url: str) -> Optional[Detection]:
    """Parse an ATS out of a URL string without any network calls."""
    if not url:
        return None

    m = WORKDAY_SITE_PAT.search(url)
    if m:
        host, path = m.group(2), m.group(3).split("/job/")[0].rstrip("/")
        tenant, site = workday_ids(host, path)
        if tenant and site:
            return Detection("workday", tenant, host, path, "high", "Workday board.",
                             url=f"https://{host}{path}")

    m = WORKDAY_PAT.search(url)
    if m:
        host = m.group(2).lower()
        path = (m.group(3) or "").split("/job/")[0].rstrip("/")
        tenant = host.split(".")[0]
        return Detection("workday", tenant, host, path or None, "high",
                         "Workday board.", url=f"https://{host}{path}")

    for ats, pat in PATTERNS:
        m = pat.search(url)
        if m:
            slug = _clean_slug(m.group(1), ats)
            # Greenhouse embed links carry the slug in ?for=
            if ats == "greenhouse" and (not slug or slug == "embed"):
                q = parse_qs(urlparse(url if "//" in url else "https://" + url).query)
                slug = _clean_slug((q.get("for") or [None])[0], ats)
            if slug:
                from .fetchers.ats import board_url
                return Detection(ats, slug, confidence="high", url=board_url(ats, slug))

    for pat, label in LINK_ONLY:
        if pat.search(url):
            return Detection("link", None, confidence="high", note=label, url=url)
    return None


def detect_from_html(html: str) -> Optional[Detection]:
    """Scan raw page source for embedded ATS references."""
    if not html:
        return None

    for pat in (WORKDAY_SITE_PAT, WORKDAY_PAT):
        m = pat.search(html)
        if m:
            d = detect_from_url(m.group(0))
            if d:
                d.confidence, d.note = "medium", "Found Workday in page source."
                return d

    # Count hits so a stray link doesn't beat the real board.
    best: Optional[Detection] = None
    best_count = 0
    for ats, pat in PATTERNS:
        hits = [s for s in (_clean_slug(m.group(1), ats) for m in pat.finditer(html)) if s]
        if not hits:
            continue
        slug = max(set(hits), key=hits.count)
        count = hits.count(slug)
        if count > best_count:
            from .fetchers.ats import board_url
            best, best_count = Detection(ats, slug, confidence="medium",
                                         note="Found in careers page source.",
                                         url=board_url(ats, slug)), count
    if best:
        return best
    for pat, label in LINK_ONLY:
        m = pat.search(html)
        if m:
            # Pull the whole URL around the match for a usable link.
            um = re.search(r"https?://[^\s\"'<>]*" + re.escape(m.group(0)) + r"[^\s\"'<>]*", html)
            return Detection("link", None, confidence="medium", note=label,
                             url=um.group(0) if um else None)
    return None


# --- Live probes -----------------------------------------------------------
# Each requires at least one live posting: an empty 200 proves nothing.

def _get(url: str, **kw):
    return requests.get(url, headers=HEADERS, timeout=config.HTTP_TIMEOUT, **kw)


def probe_greenhouse(slug: str) -> bool:
    try:
        r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        return r.status_code == 200 and bool(r.json().get("jobs"))
    except Exception:
        return False


def probe_lever(slug: str) -> bool:
    try:
        r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1")
        if r.status_code != 200:
            return False
        data = r.json()
        return isinstance(data, list) and len(data) > 0
    except Exception:
        return False


def probe_ashby(slug: str) -> bool:
    try:
        r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        return r.status_code == 200 and bool(r.json().get("jobs"))
    except Exception:
        return False


def probe_smartrecruiters(slug: str) -> bool:
    """SmartRecruiters returns HTTP 200 with an empty result set for company
    slugs that don't exist, so status alone proves nothing."""
    try:
        r = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
        if r.status_code != 200:
            return False
        data = r.json()
        return isinstance(data, dict) and (data.get("totalFound", 0) > 0 or bool(data.get("content")))
    except Exception:
        return False


def probe_workable(slug: str) -> bool:
    try:
        r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
        return r.status_code == 200 and bool(r.json().get("jobs"))
    except Exception:
        return False


def probe_recruitee(slug: str) -> bool:
    try:
        r = _get(f"https://{slug}.recruitee.com/api/offers/")
        return r.status_code == 200 and bool(r.json().get("offers"))
    except Exception:
        return False


def probe_bamboohr(slug: str) -> bool:
    try:
        # Unknown subdomains redirect to bamboohr.com's marketing site.
        r = _get(f"https://{slug}.bamboohr.com/careers/list", allow_redirects=False)
        return r.status_code == 200 and bool(r.json().get("result"))
    except Exception:
        return False


def probe_breezy(slug: str) -> bool:
    try:
        r = _get(f"https://{slug}.breezy.hr/json", allow_redirects=False)
        data = r.json() if r.status_code == 200 else None
        return isinstance(data, list) and len(data) > 0
    except Exception:
        return False


def probe_workday(slug: str, workday_host: str | None = None,
                  workday_path: str | None = None) -> bool:
    """Verify a Workday tenant+site actually serves postings. A wrong site id
    still returns HTTP 200 with an empty set, so require postings."""
    if not workday_host:
        return False
    try:
        tenant, site = workday_ids(workday_host, workday_path)
        if not (tenant and site):
            return False
        r = requests.post(
            f"https://{workday_host}/wday/cxs/{tenant}/{site}/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={"User-Agent": config.USER_AGENT, "Content-Type": "application/json"},
            timeout=config.HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return False
        data = r.json()
        return isinstance(data, dict) and (data.get("total", 0) > 0 or bool(data.get("jobPostings")))
    except Exception:
        return False


# Guessable-by-name boards, most common first. Workday isn't guessable
# (tenant host and site id are arbitrary), so it's only ever confirmed from a URL.
PROBES = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "ashby": probe_ashby,
    "bamboohr": probe_bamboohr,
    "workable": probe_workable,
    "smartrecruiters": probe_smartrecruiters,
    "breezy": probe_breezy,
    "recruitee": probe_recruitee,
}


def probe_detection(d: Detection) -> bool:
    """Confirm a Detection against the live board."""
    if d.ats == "workday":
        return probe_workday(d.slug, d.workday_host, d.workday_path)
    fn = PROBES.get(d.ats)
    return bool(fn and d.slug and fn(d.slug))


def probe_row(row) -> Optional[bool]:
    """Probe a companies-table row. None when the ATS has no probe."""
    ats = row["ats"]
    if ats == "workday":
        return probe_workday(row["slug"], row["workday_host"], row["workday_path"])
    fn = PROBES.get(ats)
    if not fn:
        return None
    return fn(row["slug"])


def slug_guesses(company_name: str, careers_url: str | None = None) -> list[str]:
    """Generate plausible slugs, best guess first."""
    name = company_name.strip().lower()
    name = re.sub(r"&", " and ", name)
    base = re.sub(r"[^a-z0-9\s\-]", "", name)
    compact = re.sub(r"[\s\-]+", "", base)
    hyphen = re.sub(r"[\s]+", "-", base.strip())

    guesses = [compact, hyphen]

    # Drop corporate suffixes: "Acme Software Inc" -> "acme"
    stripped = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|"
                      r"technology|labs|software|systems|group|holdings|the)\b", "", base)
    stripped_compact = re.sub(r"[\s\-]+", "", stripped)
    if stripped_compact and stripped_compact not in guesses:
        guesses.append(stripped_compact)
    stripped_hyphen = re.sub(r"[\s]+", "-", stripped.strip())
    if stripped_hyphen and stripped_hyphen not in guesses:
        guesses.append(stripped_hyphen)

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
    return out[:5]


def norm_company(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|"
               r"technology|labs|software|systems|group|holdings|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


_norm_company = norm_company   # back-compat name used by older modules


def detect(company_name: str, careers_url: str | None = None,
           probe: bool = True, verify: bool = True, max_guesses: int = 5) -> Detection:
    """Full detection pipeline for one company.

    Every step before the last is *evidence* (with `verify`, a found board is
    still confirmed live). The last step is a guess, only accepted if a live
    board confirms it.

    Known limit: this only sees raw HTML. Careers pages that render their job
    list with JavaScript hide the ATS link, so we fall through to guessing —
    which can't find Workday. The board finder (boardfinder.py) adds web
    search and AI on top of this for those.
    """
    found_link: Optional[Detection] = None

    # 1. The URL itself already names the ATS.
    if careers_url:
        d = detect_from_url(careers_url)
        if d and d.ats != "link":
            if not verify or probe_detection(d):
                return d
        elif d:
            found_link = d

    # 2. The careers page: redirect target, then page source.
    if careers_url:
        try:
            url = careers_url if "//" in careers_url else f"https://{careers_url}"
            r = requests.get(url, headers={"User-Agent": config.BROWSER_UA,
                                           "Accept": "text/html,*/*"},
                             timeout=config.HTTP_TIMEOUT, allow_redirects=True)
            d = detect_from_url(r.url)
            if d and d.ats != "link" and (not verify or probe_detection(d)):
                d.note = "Careers page redirects to the board."
                return d
            d2 = detect_from_html(r.text)
            if d2 and d2.ats != "link" and (not verify or probe_detection(d2)):
                return d2
            found_link = found_link or (d if d and d.ats == "link" else None) or \
                (d2 if d2 and d2.ats == "link" else None)
        except Exception:
            pass

    # 3. Last resort: guess slugs, and require a live board to confirm.
    #    All (slug, ATS) probes run in parallel; the best-ranked hit wins.
    if probe:
        guesses = slug_guesses(company_name, careers_url)[:max_guesses]
        pairs = [(slug, ats) for slug in guesses for ats in PROBES]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda p: PROBES[p[1]](p[0]), pairs))
        for (slug, ats), ok in zip(pairs, results):
            if ok:
                from .fetchers.ats import board_url
                return Detection(ats, slug, confidence="medium",
                                 note=f"Guessed and confirmed against the live board ({ats}/{slug}).",
                                 url=board_url(ats, slug))

    if found_link:
        found_link.note = (f"Uses {found_link.note}, which can't be read automatically — "
                           "but here's the link.")
        return found_link
    return Detection("unknown", None, confidence="low",
                     note="No supported job board found.")
