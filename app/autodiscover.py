"""Auto-discover ATS boards for companies the job sites found near you.

Job sites are the wide net, but their listings lag and go through redirect
links. A company's own board is the fast lane: near-real-time and canonical.
This module promotes job-site finds into tracked companies.

Two passes per refresh:

  A. Apply-link promotion (cheap, high-yield, the big v3 change). JobSpy now
     returns Indeed's direct apply link, which for most employers points
     straight at their Greenhouse/Lever/Workday/... board. Parsing that URL
     costs nothing; one probe confirms the board is live. This is the only
     automatic way to find Workday boards (tenant + site can't be guessed) —
     so companies like Domo get picked up without you lifting a finger.
     Up to APPLY_LINK_LIMIT per refresh.

  B. Name guessing (the old method) for in-range companies without a usable
     apply link, limited by the discover_per_refresh setting, ranked by how
     many openings they have near you.

Confirm-or-nothing: a company is added only when a live board answers with
jobs. Every attempt is remembered in the meta table so the same company isn't
re-probed each refresh; misses retry after RETRY_DAYS.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db, detect
from .fetchers import ats as ats_fetchers

RETRY_DAYS = 30
APPLY_LINK_LIMIT = 25
META_PREFIX = "discover:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _attempted_recently(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (META_PREFIX + key,)
    ).fetchone()
    if not row:
        return False
    try:
        rec = json.loads(row["value"])
    except Exception:
        return True
    if rec.get("status") == "found":
        return True
    try:
        at = datetime.fromisoformat(rec["at"])
    except Exception:
        return True
    return _now() - at < timedelta(days=RETRY_DAYS)


def _record_attempt(conn: sqlite3.Connection, key: str, name: str, status: str):
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (META_PREFIX + key,
         json.dumps({"status": status, "name": name, "at": _now().isoformat()})),
    )
    conn.commit()


_PLACEHOLDER = detect.norm_company("Unknown company")


def _eligible(job: dict) -> bool:
    return job.get("geo_status") in ("in_range", "remote")


def _tracked(conn: sqlite3.Connection) -> tuple[set, set]:
    names, boards = set(), set()
    for r in conn.execute("SELECT name, ats, slug, workday_host FROM companies").fetchall():
        names.add(detect.norm_company(r["name"]))
        boards.add((r["ats"], (r["slug"] or "").lower(), r["workday_host"]))
    return names, boards


def candidates(conn: sqlite3.Connection, agg_jobs: list[dict],
               limit: int) -> list[tuple[str, str | None, int]]:
    """Untracked, in-range companies from this run's job-site results, without
    a usable apply link (those go through pass A). Returns up to `limit` of
    (name, company_url, posting_count), most active first."""
    tracked, _ = _tracked(conn)
    by_norm: dict[str, dict] = {}
    for job in agg_jobs:
        if not _eligible(job):
            continue
        name = (job.get("company_name") or "").strip()
        norm = detect.norm_company(name)
        if not norm or norm == _PLACEHOLDER or norm in tracked:
            continue
        rec = by_norm.setdefault(norm, {"name": name, "url": None, "count": 0})
        rec["count"] += 1
        if not rec["url"] and job.get("company_url"):
            rec["url"] = job["company_url"]

    ranked = sorted(by_norm.items(), key=lambda kv: -kv[1]["count"])
    out = []
    for norm, rec in ranked:
        if _attempted_recently(conn, norm):
            continue
        out.append((rec["name"], rec["url"], rec["count"]))
        if len(out) >= limit:
            break
    return out


def _add_and_fetch(conn, name, d: detect.Detection, careers_url=None):
    url = d.url or ats_fetchers.board_url(d.ats, d.slug, d.workday_host, d.workday_path)
    cid = db.add_company(conn, name, careers_url or url, d.ats, d.slug,
                         d.workday_host, d.workday_path, board_url=url, added_by="auto")
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    try:
        jobs = ats_fetchers.fetch_company(row["name"], row["ats"], row["slug"], row)
    except Exception:
        jobs = []
    return row, jobs


def promote_apply_links(conn: sqlite3.Connection, agg_jobs: list[dict],
                        limit: int = APPLY_LINK_LIMIT, on_progress=None):
    """Pass A: confirm boards named by job-site apply links."""
    tracked_names, tracked_boards = _tracked(conn)
    seen: set = set()
    todo = []
    for job in agg_jobs:
        if not _eligible(job) or not job.get("apply_url"):
            continue
        d = detect.detect_from_url(job["apply_url"])
        if not d or not d.supported:
            continue
        key = (d.ats, (d.slug or "").lower(), d.workday_host)
        name = (job.get("company_name") or "").strip()
        if key in seen or key in tracked_boards or detect.norm_company(name) in tracked_names:
            continue
        seen.add(key)
        mkey = f"board:{d.ats}:{(d.slug or '').lower()}:{d.workday_host or ''}"
        if _attempted_recently(conn, mkey):
            continue
        todo.append((name, d, job.get("company_url"), mkey))
        if len(todo) >= limit:
            break

    found = []
    for i, (name, d, curl, mkey) in enumerate(todo):
        if on_progress:
            on_progress(i, len(todo), f"Checking {name}'s {d.ats} board…")
        if detect.probe_detection(d):
            row, jobs = _add_and_fetch(conn, name, d, None)
            _record_attempt(conn, mkey, name, "found")
            _record_attempt(conn, detect.norm_company(name), name, "found")
            found.append((row, jobs))
        else:
            _record_attempt(conn, mkey, name, "none")
    return found


def run(conn: sqlite3.Connection, agg_jobs: list[dict], limit: int,
        on_progress=None) -> list[tuple[sqlite3.Row, list[dict]]]:
    """Both passes. Returns [(company_row, jobs)] for the caller to ingest, so
    newly discovered companies contribute jobs in the same refresh."""
    found = promote_apply_links(conn, agg_jobs, on_progress=on_progress)

    cands = candidates(conn, agg_jobs, limit)
    for i, (name, url, _count) in enumerate(cands):
        if on_progress:
            on_progress(i, len(cands), f"Looking for {name}'s job board…")
        norm = detect.norm_company(name)
        try:
            d = detect.detect(name, careers_url=url, probe=True, max_guesses=2)
        except Exception:
            d = None
        if not d or not d.supported:
            _record_attempt(conn, norm, name, "none")
            continue
        row, jobs = _add_and_fetch(conn, name, d, url)
        _record_attempt(conn, norm, name, "found")
        found.append((row, jobs))
    return found
