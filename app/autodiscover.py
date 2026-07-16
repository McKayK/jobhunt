"""Auto-discover ATS boards for companies the aggregators found near you.

The aggregate sites (Indeed & co.) are the wide net: they surface companies
you've never heard of, but their listings lag by hours-to-days and go through
their redirect links. Direct ATS boards are the fast lane: near-real-time and
canonical. This module bridges the two — when an aggregator finds an in-range
posting from an untracked company, we try to detect that company's ATS board
and, if confirmed live, add it to the tracked list so every future refresh
pulls it directly.

Detection is confirm-or-nothing: a company is only added when a live board
answers for it (detect.py probes the actual APIs), so false positives don't
pollute the list. Every attempt — hit or miss — is recorded in the meta table
so we never re-probe the same company on every refresh. Misses are retried
after RETRY_DAYS in case the company adopts a supported ATS later.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db, detect
from .fetchers import ats as ats_fetchers

RETRY_DAYS = 30
META_PREFIX = "discover:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _attempted_recently(conn: sqlite3.Connection, norm: str) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (META_PREFIX + norm,)
    ).fetchone()
    if not row:
        return False
    try:
        rec = json.loads(row["value"])
    except Exception:
        return True
    if rec.get("status") == "found":
        return True          # already promoted once; never re-probe
    try:
        at = datetime.fromisoformat(rec["at"])
    except Exception:
        return True
    return _now() - at < timedelta(days=RETRY_DAYS)


def _record_attempt(conn: sqlite3.Connection, norm: str, name: str, status: str):
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (META_PREFIX + norm,
         json.dumps({"status": status, "name": name, "at": _now().isoformat()})),
    )
    conn.commit()


_PLACEHOLDER = detect._norm_company("Unknown company")

def candidates(conn: sqlite3.Connection, agg_jobs: list[dict],
               limit: int) -> list[tuple[str, str | None, int]]:
    """Untracked, in-range companies from this run's aggregate results.

    Returns up to `limit` of (name, company_url, posting_count), most active
    companies first — if a company has five openings near you, its board is
    worth more than one with a single posting.
    """
    tracked = {
        detect._norm_company(r["name"])
        for r in conn.execute("SELECT name FROM companies").fetchall()
    }

    by_norm: dict[str, dict] = {}
    for job in agg_jobs:
        if job.get("geo_status") != "in_range":
            continue
        name = (job.get("company_name") or "").strip()
        norm = detect._norm_company(name)
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


def run(conn: sqlite3.Connection, agg_jobs: list[dict], limit: int,
        on_progress=None) -> list[tuple[sqlite3.Row, list[dict]]]:
    """Probe candidates; add confirmed boards and fetch them immediately.

    Returns [(company_row, jobs)] for the caller to ingest, so newly
    discovered companies contribute jobs in the same refresh that found them.
    """
    found: list[tuple[sqlite3.Row, list[dict]]] = []
    cands = candidates(conn, agg_jobs, limit)
    for i, (name, url, _count) in enumerate(cands):
        if on_progress:
            on_progress(i, len(cands), f"Checking {name}…")
        norm = detect._norm_company(name)
        try:
            d = detect.detect(name, careers_url=url, probe=True)
        except Exception:
            d = None
        if not d or d.ats == "unknown" or not (d.slug or d.workday_host):
            _record_attempt(conn, norm, name, "none")
            continue

        cid = db.add_company(conn, name, url, d.ats, d.slug,
                             d.workday_host, d.workday_path)
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
        _record_attempt(conn, norm, name, "found")
        try:
            jobs = ats_fetchers.fetch_company(row["name"], row["ats"], row["slug"], row)
        except Exception:
            jobs = []
        found.append((row, jobs))
        if on_progress:
            on_progress(i + 1, len(cands), f"Added {name} ({d.ats})")
    return found
