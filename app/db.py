"""SQLite access layer. Single-user, so no pooling ceremony required."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Sources that are search results, not a company's complete board. A posting
# missing from one search doesn't mean it closed (the search is capped and
# ranked), so these expire by age instead of by absence.
AGGREGATE_SOURCES = {
    "indeed", "zip_recruiter", "google", "linkedin", "glassdoor",
    "himalayas", "jobicy", "remotive", "remoteok",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ago_iso(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# --- Word-boundary keyword matching ------------------------------------------
# SQL LIKE '%intern%' also hides "Internal Tools Engineer" and "International
# Sales". Keywords are matched on word boundaries instead, via a function we
# register on every connection.

_KW_CACHE: dict[str, re.Pattern] = {}


def _kw_pattern(kw: str) -> re.Pattern:
    p = _KW_CACHE.get(kw)
    if p is None:
        # \b only works next to word characters; keywords like "c++" or ".net"
        # get a looser lookaround instead.
        left = r"\b" if re.match(r"\w", kw) else r"(?<!\w)"
        right = r"\b" if re.search(r"\w$", kw) else r"(?!\w)"
        p = re.compile(left + re.escape(kw).replace(r"\ ", r"[\s\-/]+") + right, re.I)
        _KW_CACHE[kw] = p
    return p


def kw_match(text: Optional[str], kw: Optional[str]) -> int:
    if not text or not kw:
        return 0
    return 1 if _kw_pattern(kw.strip().lower()).search(text) else 0


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.create_function("KW", 2, kw_match, deterministic=True)
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    # Migrations for databases created before these columns existed.
    _add_column_if_missing(conn, "geocache", "country", "TEXT")
    for col, decl in [
        ("geo_status", "TEXT"), ("country", "TEXT"),
        ("apply_url", "TEXT"), ("company_url", "TEXT"),
        ("salary_text", "TEXT"), ("job_type", "TEXT"),
        ("risk_score", "INTEGER NOT NULL DEFAULT 0"), ("risk_flags", "TEXT"),
        ("is_staffing", "INTEGER NOT NULL DEFAULT 0"),
        ("match_score", "INTEGER"), ("match_detail", "TEXT"),
        ("app_status", "TEXT"), ("status_at", "TEXT"),
    ]:
        _add_column_if_missing(conn, "jobs", col, decl)
    for col, decl in [("board_url", "TEXT"), ("added_by", "TEXT")]:
        _add_column_if_missing(conn, "companies", col, decl)
    # Applied jobs from before the pipeline existed start at 'applied'.
    conn.execute("UPDATE jobs SET app_status = 'applied', status_at = applied_at "
                 "WHERE applied_at IS NOT NULL AND app_status IS NULL")
    conn.commit()


# --- Job identity ----------------------------------------------------------

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")
# Title noise that varies between sources for the same posting.
_TITLE_NOISE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|full[- ]?time|part[- ]?time|contract|w2|c2c)\b", re.I
)
_REQ_ID = re.compile(r"\b(req|job|jr|r)[-#\s]?\d{3,}\b", re.I)
_CO_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|holdings|group|the)\b")


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.lower()
    s = _REQ_ID.sub(" ", s)
    s = _TITLE_NOISE.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def norm_company(s: Optional[str]) -> str:
    """'Lucid Software, Inc.' and 'Lucid Software' are the same employer."""
    n = _norm(s)
    n = _CO_SUFFIX.sub(" ", n)
    return _WS.sub(" ", n).strip()


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
    # NOTE: must stay byte-for-byte compatible with older versions — changing
    # it would make every stored job look new and orphan your applied marks.
    key = "|".join([_norm(company_name), _norm(title), _canon_location(location_raw)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# --- Upsert ----------------------------------------------------------------

def upsert_job(conn: sqlite3.Connection, job: dict) -> bool:
    """Insert a job or refresh last_seen_at. Returns True if it's new.

    Never touches seen_at/applied_at/hidden/starred/notes — user state is sacred.

    When a job first found on a job site later shows up on the company's own
    board, the board wins: its URL, source, and company link replace the
    aggregator's, so the row follows the board's (more reliable) lifecycle.
    """
    h = job_hash(job["company_name"], job["title"], job.get("location_raw"))
    ts = now_iso()
    from_board = job.get("company_id") is not None and job["source"] not in AGGREGATE_SOURCES

    existing = conn.execute(
        "SELECT id, source, company_id FROM jobs WHERE job_hash = ?", (h,)).fetchone()
    if existing:
        upgrade = from_board and existing["source"] in AGGREGATE_SOURCES
        # A job-site re-sighting must not overwrite a company-board URL.
        keep_url = (existing["source"] not in AGGREGATE_SOURCES
                    and job["source"] in AGGREGATE_SOURCES)
        if keep_url:
            # The company's own board is authoritative for this posting's
            # lifecycle; a job site only gets to fill in missing details.
            conn.execute(
                """UPDATE jobs SET apply_url = COALESCE(apply_url, ?),
                                   company_url = COALESCE(company_url, ?),
                                   salary_text = COALESCE(salary_text, ?)
                    WHERE id = ?""",
                (job.get("apply_url"), job.get("company_url"),
                 job.get("salary_text"), existing["id"]))
            return False
        url_sql = "?" if upgrade else "COALESCE(?, url)"
        conn.execute(
            f"""UPDATE jobs
                  SET last_seen_at = ?, gone_at = NULL,
                      url = {url_sql},
                      source = {'?' if upgrade else 'source'},
                      company_id = COALESCE(company_id, ?),
                      apply_url = COALESCE(?, apply_url),
                      company_url = COALESCE(?, company_url),
                      description = COALESCE(?, description),
                      salary_text = COALESCE(?, salary_text),
                      job_type = COALESCE(?, job_type),
                      posted_at = COALESCE(posted_at, ?),
                      lat = ?, lon = ?, distance_miles = ?,
                      geo_status = ?, country = ?, remote = ?
                WHERE id = ?""",
            [ts, job.get("url")] + ([job["source"]] if upgrade else []) + [
             job.get("company_id"), job.get("apply_url"), job.get("company_url"),
             job.get("description"), job.get("salary_text"), job.get("job_type"),
             job.get("posted_at"),
             job.get("lat"), job.get("lon"), job.get("distance_miles"),
             job.get("geo_status"), job.get("country"),
             1 if job.get("remote") else 0, existing["id"]],
        )
        return False

    conn.execute(
        """INSERT INTO jobs
           (job_hash, company_id, company_name, title, location_raw, lat, lon,
            distance_miles, geo_status, country, remote, url, apply_url, company_url,
            description, salary_text, job_type, posted_at, source,
            first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            h, job.get("company_id"), job["company_name"], job["title"],
            job.get("location_raw"), job.get("lat"), job.get("lon"),
            job.get("distance_miles"), job.get("geo_status"), job.get("country"),
            1 if job.get("remote") else 0, job["url"], job.get("apply_url"),
            job.get("company_url"), job.get("description"), job.get("salary_text"),
            job.get("job_type"), job.get("posted_at"), job["source"], ts, ts,
        ),
    )
    return True


def mark_gone_company(conn: sqlite3.Connection, company_id: int, run_started: str) -> int:
    """A company board that answered this run is complete: anything of theirs
    we didn't see again has closed. Only called for boards that fetched OK."""
    cur = conn.execute(
        """UPDATE jobs SET gone_at = ?
            WHERE company_id = ? AND gone_at IS NULL AND last_seen_at < ?
              AND source NOT IN (%s)""" % ",".join("?" * len(AGGREGATE_SOURCES)),
        (now_iso(), company_id, run_started, *sorted(AGGREGATE_SOURCES)),
    )
    return cur.rowcount


def expire_aggregate(conn: sqlite3.Connection, days: int) -> int:
    """Job-site postings expire when no search has returned them for `days`."""
    cur = conn.execute(
        """UPDATE jobs SET gone_at = ?
            WHERE gone_at IS NULL AND last_seen_at < ?
              AND source IN (%s)""" % ",".join("?" * len(AGGREGATE_SOURCES)),
        (now_iso(), ago_iso(days), *sorted(AGGREGATE_SOURCES)),
    )
    return cur.rowcount


# --- Companies -------------------------------------------------------------

def add_company(conn: sqlite3.Connection, name: str, careers_url: str | None,
                ats: str, slug: str | None, workday_host: str | None = None,
                workday_path: str | None = None, board_url: str | None = None,
                added_by: str = "manual") -> int:
    """Insert or revive a company. Re-adding with better details fixes the row:
    Workday host/path and URLs are overwritten when new values are given (the
    old version kept a wrong Workday host forever)."""
    cur = conn.execute(
        """INSERT INTO companies (name, careers_url, ats, slug, workday_host,
                                  workday_path, board_url, added_by)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(ats, slug) DO UPDATE SET
               name = excluded.name,
               careers_url = COALESCE(excluded.careers_url, companies.careers_url),
               workday_host = COALESCE(excluded.workday_host, companies.workday_host),
               workday_path = COALESCE(excluded.workday_path, companies.workday_path),
               board_url = COALESCE(excluded.board_url, companies.board_url),
               last_error = NULL,
               active = 1
           RETURNING id""",
        (name, careers_url, ats, slug, workday_host, workday_path, board_url, added_by),
    )
    row = cur.fetchone()
    conn.commit()
    return row["id"]


def active_companies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Companies whose boards we can fetch (link-only bookmarks excluded)."""
    return conn.execute(
        "SELECT * FROM companies WHERE active = 1 AND slug IS NOT NULL "
        "AND ats IS NOT NULL AND ats != 'link' ORDER BY name"
    ).fetchall()


def company_by_norm(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Active companies keyed by normalized name, for linking job-site postings
    to companies you already track."""
    out = {}
    for r in conn.execute("SELECT * FROM companies WHERE active = 1").fetchall():
        out.setdefault(norm_company(r["name"]), r)
    return out


# --- Small JSON helpers --------------------------------------------------------

def loads(s: Optional[str], default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default
