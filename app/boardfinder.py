"""'Go find this company's job board and give me the link.'

Tries, in order, stopping at the first confirmed board:
  1. Evidence we already have: apply links on job-site postings from that
     company. Indeed's direct apply link usually IS the ATS board — this is
     how Workday companies (impossible to guess) get found.
  2. The detector: your URL / their website -> redirects -> page source ->
     guessed slugs probed against 8 ATSes in parallel.
  3. A web search (DuckDuckGo's HTML endpoint; no key needed, best-effort —
     it sometimes rate-limits automated requests).
  4. AI with web search, if you've added a Claude or Gemini key.

Every candidate board is confirmed live (non-empty) before it's reported as
trackable. If all we can find is a careers page on a system we can't read
(iCIMS, Paylocity, ...), you still get that link.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests

from . import ai, config, db, detect, geo, settings
from .fetchers.ats import board_url


def _result(d: Optional[detect.Detection], method: str, steps: list[str],
            careers_url: Optional[str] = None) -> dict:
    if d is None:
        return {"found": bool(careers_url), "supported": False, "ats": None,
                "board_url": None, "careers_url": careers_url, "method": method,
                "note": ("Found their careers page, but not a job board we can read."
                         if careers_url else "Couldn't find a job board for this company."),
                "steps": steps}
    url = d.url or board_url(d.ats, d.slug, d.workday_host, d.workday_path)
    return {"found": True, "supported": d.supported, "ats": d.ats, "slug": d.slug,
            "workday_host": d.workday_host, "workday_path": d.workday_path,
            "board_url": url, "careers_url": careers_url or url, "method": method,
            "system": d.note if d.ats == "link" else d.ats, "note": d.note, "steps": steps}


def _confirm_urls(urls: list[str], steps: list[str], label: str):
    """Return (confirmed supported Detection | None, first link-only url | None)."""
    link_only = None
    tried = set()
    for u in urls:
        d = detect.detect_from_url(u)
        if not d:
            continue
        if d.ats == "link":
            link_only = link_only or (d.url or u)
            continue
        key = (d.ats, d.slug, d.workday_host, d.workday_path)
        if key in tried:
            continue
        tried.add(key)
        if detect.probe_detection(d):
            steps.append(f"{label}: confirmed {d.ats} board")
            d.note = d.note or f"Found via {label}."
            return d, link_only
        steps.append(f"{label}: {d.ats}/{d.slug or d.workday_host} didn't answer with jobs")
    return None, link_only


def _ddg_search(query: str) -> list[str]:
    r = requests.get("https://html.duckduckgo.com/html/", params={"q": query},
                     headers={"User-Agent": config.BROWSER_UA, "Accept": "text/html"},
                     timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    urls = []
    for href in re.findall(r'href="([^"]+)"', r.text):
        if "duckduckgo.com/l/" in href:
            q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
            target = unquote((q.get("uddg") or [""])[0])
            if target:
                urls.append(target)
        elif href.startswith("http") and "duckduckgo.com" not in href:
            urls.append(href)
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:20]


_CAREERS_PATH = re.compile(r"/(careers?|jobs|join(-us)?|work-with-us|opportunities)(/|$|\?|#)", re.I)


def find_board(conn: sqlite3.Connection, name: str, hint_url: Optional[str] = None,
               use_search: bool = True, use_ai: bool = True) -> dict:
    name = (name or "").strip()
    steps: list[str] = []
    careers_link: Optional[str] = None

    # 0. Already tracked?
    tracked = db.company_by_norm(conn).get(db.norm_company(name))
    if tracked and tracked["ats"] != "link":
        res = _result(detect.Detection(tracked["ats"], tracked["slug"], tracked["workday_host"],
                                       tracked["workday_path"], "high", "Already tracked.",
                                       url=tracked["board_url"] or board_url(
                                           tracked["ats"], tracked["slug"],
                                           tracked["workday_host"], tracked["workday_path"])),
                      "already tracked", ["Already in your companies list."])
        res["already_tracked"] = True
        return res

    # 1. Evidence from postings we've stored.
    norm = db.norm_company(name)
    rows = conn.execute(
        "SELECT company_name, apply_url, url, company_url FROM jobs "
        "WHERE apply_url IS NOT NULL OR company_url IS NOT NULL ORDER BY last_seen_at DESC LIMIT 4000"
    ).fetchall()
    evidence, sites = [], []
    for r in rows:
        if db.norm_company(r["company_name"]) == norm:
            if r["apply_url"]:
                evidence.append(r["apply_url"])
            if r["company_url"]:
                sites.append(r["company_url"])
    if evidence:
        d, link = _confirm_urls(evidence, steps, "apply links on their postings")
        if d:
            return _result(d, "apply link on a posting", steps)
        careers_link = careers_link or link
    else:
        steps.append("No stored postings with a direct apply link")

    # 2. Detector on the URL you gave / their website.
    # Name guessing happens inside detect(), so one pass per URL is enough;
    # with no URL at all, one name-only pass.
    candidates = [u for u in [hint_url] + sites[:1] if u] or [None]
    for i, url in enumerate(candidates):
        d = detect.detect(name, url, probe=(i == 0), max_guesses=4)
        label = f"from {url}" if url else "guessing from the name"
        if d.supported:
            steps.append(f"Detector ({label}): {d.ats}")
            return _result(d, "detector", steps)
        if d.ats == "link":
            careers_link = careers_link or d.url
        steps.append(f"Detector ({label}): nothing readable")

    near = geo.zip_place_name(settings.get(conn, "home_zip"))

    # 3. Web search.
    if use_search:
        try:
            q = f'"{name}" careers jobs'
            urls = _ddg_search(q)
            urls += _ddg_search(f'"{name}" (site:greenhouse.io OR site:lever.co OR '
                                f'site:myworkdayjobs.com OR site:ashbyhq.com OR site:bamboohr.com '
                                f'OR site:workable.com OR site:icims.com OR site:paylocity.com)')
            if urls:
                d, link = _confirm_urls(urls, steps, "web search")
                if d:
                    return _result(d, "web search", steps)
                careers_link = careers_link or link
                if not careers_link:
                    for u in urls:
                        if _CAREERS_PATH.search(urlparse(u).path or "") and \
                                not re.search(r"indeed|linkedin|glassdoor|ziprecruiter|builtin|simplyhired", u):
                            careers_link = u
                            break
            else:
                steps.append("Web search returned nothing (it may be rate-limiting us)")
        except Exception as e:
            steps.append(f"Web search failed: {type(e).__name__}")

    # 4. AI with web search.
    if use_ai and ai.provider(conn):
        try:
            urls, model = ai.find_board_urls(conn, name, near)
            steps.append(f"AI ({model}) suggested {len(urls)} link(s)")
            d, link = _confirm_urls(urls, steps, "AI search")
            if d:
                return _result(d, "AI search", steps)
            careers_link = careers_link or link
            if not careers_link:
                for u in urls:
                    if _CAREERS_PATH.search(urlparse(u).path or ""):
                        careers_link = u
                        break
                careers_link = careers_link or (urls[0] if urls else None)
        except ai.AIError as e:
            steps.append(f"AI step failed: {e}")
    elif use_ai:
        steps.append("AI step skipped (no API key)")

    return _result(None, "not found", steps, careers_link or hint_url or (sites[0] if sites else None))
