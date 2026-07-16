"""Find a company's real ATS identifier by rendering its careers page.

Why this exists
---------------
SmartRecruiters' own docs say the company identifier is "the identifier as it
appears at the end of the default career site URL", and that some companies use
identifiers unrelated to their brand name (TheNielsenCompany, not Nielsen).
There is no public company-search endpoint, so an identifier cannot be looked up
by name and cannot be reliably guessed. The docs' recommended method is to
inspect network requests in DevTools.

Most careers pages render their job list with JavaScript, so a plain
requests.get() sees a shell with no ATS link in it. That is why raw-HTML
detection reported "unknown" for ~48 companies that do have real boards.

This module drives a real browser, lets the page run its scripts, and watches
where it actually calls. The ATS identifier shows up in those requests.

Usage:
    pip install playwright
    playwright install chromium

    python -m app.discover "Nu Skin" https://www.nuskin.com/careers
    python -m app.discover --batch companies.txt
    python -m app.discover --dead        # re-discover unreachable companies in the DB
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Optional

from . import db, detect as detect_mod

# Reuse the same URL patterns the static detector uses; these are what we look
# for in network traffic rather than in page source.
ATS_URL_HINTS = [
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_\-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_\-]+)", re.I)),
    ("lever",      re.compile(r"api\.lever\.co/v\d/postings/([a-z0-9_\-]+)", re.I)),
    ("lever",      re.compile(r"jobs\.lever\.co/([a-z0-9_\-]+)", re.I)),
    ("ashby",      re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_\-\.]+)", re.I)),
    # SmartRecruiters: the API call itself carries the true identifier, and it is
    # case-sensitive, so do NOT lowercase what we capture.
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_\-\.]+)/postings")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_\-\.]+)")),
    ("workable",   re.compile(r"apply\.workable\.com/(?:api/v\d/accounts/)?([a-z0-9_\-]+)", re.I)),
    ("recruitee",  re.compile(r"([a-z0-9\-]+)\.recruitee\.com", re.I)),
]

WORKDAY_HINT = re.compile(
    r"([a-z0-9\-]+\.wd\d+\.myworkdayjobs\.com)(/(?:wday/cxs/[^/]+/)?([A-Za-z0-9_\-]+))?", re.I
)

BLOCK = {"embed", "job_board", "jobs", "careers", "www", "api", "static",
         "assets", "images", "css", "js", "v1", "v0", "boards", "search",
         "company", "postings", "widget", "cdn"}


@dataclass
class Found:
    ats: str
    slug: str
    evidence: str
    workday_host: Optional[str] = None
    workday_path: Optional[str] = None


def _scan(url: str) -> Optional[Found]:
    m = WORKDAY_HINT.search(url)
    if m:
        return Found("workday", m.group(1).split(".")[0], url[:120],
                     workday_host=m.group(1), workday_path=m.group(3))
    for ats, pat in ATS_URL_HINTS:
        m = pat.search(url)
        if m:
            slug = m.group(1)
            # SmartRecruiters identifiers are case-sensitive; others are not.
            cmp_slug = slug if ats == "smartrecruiters" else slug.lower()
            if cmp_slug.lower() in BLOCK or len(cmp_slug) < 2:
                continue
            return Found(ats, cmp_slug, url[:120])
    return None


def discover(company_name: str, careers_url: str, timeout_ms: int = 25000,
             headless: bool = True, verbose: bool = True) -> Optional[Found]:
    """Render the careers page and watch where it calls."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run:\n"
              "  pip install playwright\n  playwright install chromium")
        sys.exit(1)

    hits: list[Found] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36")
        )
        page = ctx.new_page()

        def on_request(req):
            f = _scan(req.url)
            if f:
                hits.append(f)

        page.on("request", on_request)
        page.on("response", lambda r: (lambda f: hits.append(f) if f else None)(_scan(r.url)))

        url = careers_url if "//" in careers_url else f"https://{careers_url}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Give client-side job widgets time to fire their API calls.
            page.wait_for_timeout(4000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # The final URL may itself be the ATS (careers pages often redirect).
            f = _scan(page.url)
            if f:
                hits.append(f)

            # Anchors can point at the board even when no XHR fires.
            try:
                for href in page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)")[:400]:
                    f = _scan(href)
                    if f:
                        hits.append(f)
            except Exception:
                pass

            # Last resort: the rendered DOM, which raw HTML fetch never saw.
            try:
                f2 = detect_mod.detect_from_html(page.content())
                if f2 and f2.slug:
                    hits.append(Found(f2.ats, f2.slug, "rendered DOM"))
            except Exception:
                pass
        except Exception as e:
            if verbose:
                print(f"    page error: {type(e).__name__}: {str(e)[:80]}")
        finally:
            browser.close()

    if not hits:
        return None

    # Prefer a hit that a live board actually confirms.
    for h in hits:
        probe = detect_mod.PROBES.get(h.ats)
        if probe and probe(h.slug):
            return h
    # Workday has no probe yet, but the host is still worth recording.
    for h in hits:
        if h.ats == "workday":
            return h
    return hits[0]


def _print(name: str, f: Optional[Found]):
    if not f:
        print(f"  [NONE] {name[:30]:<32} nothing found")
        return
    extra = f" host={f.workday_host}{f.workday_path or ''}" if f.workday_host else ""
    print(f"  [FOUND] {name[:29]:<31} {f.ats}/{f.slug}{extra}")
    print(f"          via {f.evidence}")


def main():
    ap = argparse.ArgumentParser(prog="discover")
    ap.add_argument("name", nargs="?")
    ap.add_argument("url", nargs="?")
    ap.add_argument("--batch", help="file with 'Name | url' lines")
    ap.add_argument("--dead", action="store_true",
                    help="re-discover companies in the DB with no reachable board")
    ap.add_argument("--save", action="store_true", help="write results to the DB")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    args = ap.parse_args()

    targets: list[tuple[str, str]] = []

    if args.dead:
        conn = db.connect()
        db.init_db(conn)
        rows = conn.execute(
            "SELECT * FROM companies WHERE active = 1 AND careers_url IS NOT NULL"
        ).fetchall()
        from .fetchers import ats as ats_fetchers
        for r in rows:
            probe = detect_mod.PROBES.get(r["ats"])
            if probe and probe(r["slug"]):
                continue
            try:
                if ats_fetchers.fetch_company(r["name"], r["ats"], r["slug"], r):
                    continue
            except Exception:
                pass
            targets.append((r["name"], r["careers_url"]))
        conn.close()
        print(f"{len(targets)} companies need discovery\n")
    elif args.batch:
        for line in open(args.batch):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) > 1 and parts[1]:
                targets.append((parts[0], parts[1]))
    elif args.name and args.url:
        targets = [(args.name, args.url)]
    else:
        ap.error("give a name and url, or --batch FILE, or --dead")

    conn = db.connect() if args.save else None
    if conn:
        db.init_db(conn)

    found = 0
    for name, url in targets:
        f = discover(name, url, headless=not args.show)
        _print(name, f)
        if f:
            found += 1
        if conn and f and f.ats in ("greenhouse", "lever", "ashby", "smartrecruiters"):
            conn.execute(
                "UPDATE companies SET active = 0 WHERE name = ? AND active = 1", (name,)
            )
            conn.commit()
            db.add_company(conn, name, url, f.ats, f.slug)
            print(f"          saved")

    print(f"\n{found}/{len(targets)} discovered")
    if not args.save and found:
        print("Re-run with --save to write these to the database.")
    if conn:
        conn.close()


if __name__ == "__main__":
    main()