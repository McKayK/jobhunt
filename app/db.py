"""SQLite access layer. Single-user, so no pooling ceremony required."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    # Migrations for databases created before these columns existed.
    _add_column_if_missing(conn, "geocache", "country", "TEXT")
    _add_column_if_missing(conn, "jobs", "geo_status", "TEXT")
    _add_column_if_missing(conn, "jobs", "country", "TEXT")
    conn.commit()


# --- Job identity ----------------------------------------------------------

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")
# Title noise that varies between sources for the same posting.
_TITLE_NOISE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|full[- ]?time|part[- ]?time|contract|w2|c2c)\b", re.I
)
_REQ_ID = re.compile(r"\b(req|job|jr|r)[-#\s]?\d{3,}\b", re.I)


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.lower()
    s = _REQ_ID.sub(" ", s)
    s = _TITLE_NOISE.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _norm_location(s: Optional[str]) -> str:
    """Collapse a location to city+state so 'Lindon, UT' == 'Lindon, Utah, US'."""
    if not s:
        return ""
    n = _norm(s)
    if re.search(r"\b(remote|anywhere|work from home|wfh)\b", n):
        return "remote"
    # Keep the first two comma-ish segments; drop country/zip tails.
    parts = [p.strip() for p in re.split(r"[,/|]", s) if p.strip()]
    parts = [p for p in parts if not re.fullmatch(r"\d{5}(-\d{4})?", p.strip())]
    parts = [p for p in parts if _norm(p) not in ("us", "usa", "united states")]
    return _norm(" ".join(parts[:2]))


_STATE_ABBR = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga",
    "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "west virginia": "wv", "wisconsin": "wi",
    "wyoming": "wy", "district of columbia": "dc",
}


def _canon_location(s: Optional[str]) -> str:
    n = _norm_location(s)
    for full, abbr in _STATE_ABBR.items():
        n = re.sub(rf"\b{re.escape(full)}\b", abbr, n)
    return _WS.sub(" ", n).strip()


def job_hash(company_name: str, title: str, location_raw: Optional[str]) -> str:
    """Stable identity for a posting, independent of which source found it.

    Deliberately excludes posted_at: sources disagree on it, and a job that
    gets re-dated shouldn't reappear as new.
    """
    key = "|".join([_norm(company_name), _norm(title), _canon_location(location_raw)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# --- Upsert ----------------------------------------------------------------

def upsert_job(conn: sqlite3.Connection, job: dict) -> bool:
    """Insert a job or refresh last_seen_at. Returns True if it's new.

    Never touches seen_at/applied_at/hidden/starred/notes — user state is sacred.
    """
    h = job_hash(job["company_name"], job["title"], job.get("location_raw"))
    ts = now_iso()

    existing = conn.execute("SELECT id, gone_at FROM jobs WHERE job_hash = ?", (h,)).fetchone()
    if existing:
        # Refresh volatile fields; a job that reappeared is no longer gone.
        conn.execute(
            """UPDATE jobs
                  SET last_seen_at = ?, gone_at = NULL,
                      url = COALESCE(?, url),
                      description = COALESCE(?, description),
                      posted_at = COALESCE(posted_at, ?),
                      lat = ?, lon = ?, distance_miles = ?,
                      geo_status = ?, country = ?, remote = ?
                WHERE id = ?""",
            (ts, job.get("url"), job.get("description"), job.get("posted_at"),
             job.get("lat"), job.get("lon"), job.get("distance_miles"),
             job.get("geo_status"), job.get("country"),
             1 if job.get("remote") else 0, existing["id"]),
        )
        return False

    conn.execute(
        """INSERT INTO jobs
           (job_hash, company_id, company_name, title, location_raw, lat, lon,
            distance_miles, geo_status, country, remote, url, description,
            posted_at, source, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            h,
            job.get("company_id"),
            job["company_name"],
            job["title"],
            job.get("location_raw"),
            job.get("lat"),
            job.get("lon"),
            job.get("distance_miles"),
            job.get("geo_status"),
            job.get("country"),
            1 if job.get("remote") else 0,
            job["url"],
            job.get("description"),
            job.get("posted_at"),
            job["source"],
            ts,
            ts,
        ),
    )
    return True


def mark_gone(conn: sqlite3.Connection, source: str, run_started: str) -> int:
    """Flag jobs from a source that didn't show up in the latest successful run."""
    cur = conn.execute(
        """UPDATE jobs SET gone_at = ?
            WHERE source = ? AND gone_at IS NULL AND last_seen_at < ?""",
        (now_iso(), source, run_started),
    )
    return cur.rowcount


# --- Companies -------------------------------------------------------------

def add_company(conn: sqlite3.Connection, name: str, careers_url: str | None,
                ats: str, slug: str | None, workday_host: str | None = None,
                workday_path: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO companies (name, careers_url, ats, slug, workday_host, workday_path)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(ats, slug) DO UPDATE SET
               name = excluded.name,
               careers_url = COALESCE(excluded.careers_url, companies.careers_url),
               active = 1
           RETURNING id""",
        (name, careers_url, ats, slug, workday_host, workday_path),
    )
    row = cur.fetchone()
    conn.commit()
    return row["id"]


def active_companies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM companies WHERE active = 1 AND slug IS NOT NULL ORDER BY name"
    ).fetchall()
