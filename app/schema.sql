-- Companies you'd actually work for, with their ATS slug once detected.
CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    careers_url   TEXT,                -- what you pasted in
    ats           TEXT,                -- greenhouse | lever | ashby | smartrecruiters | workday | unknown
    slug          TEXT,                -- board identifier for the ATS
    workday_host  TEXT,                -- workday only: e.g. company.wd5.myworkdayjobs.com
    workday_path  TEXT,                -- workday only: e.g. /en-US/External
    active        INTEGER NOT NULL DEFAULT 1,
    last_fetch_at TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ats, slug)
);

-- One row per distinct posting. job_hash is stable across sources so the same
-- job seen via Greenhouse and Adzuna collapses into one row.
CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_hash       TEXT NOT NULL UNIQUE,
    company_id     INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    company_name   TEXT NOT NULL,
    title          TEXT NOT NULL,
    location_raw   TEXT,
    lat            REAL,
    lon            REAL,
    distance_miles REAL,
    geo_status     TEXT,               -- remote | in_range | too_far | foreign | unknown
    country        TEXT,               -- ISO2 lowercase when resolved
    remote         INTEGER NOT NULL DEFAULT 0,
    url            TEXT NOT NULL,
    description    TEXT,
    posted_at      TEXT,               -- ISO8601, from the source when available
    source         TEXT NOT NULL,      -- greenhouse | lever | ashby | adzuna | usajobs | jobspy
    first_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    gone_at        TEXT,               -- set when it stops appearing in the feed
    -- Per-job user state. This is what makes "new" reliable.
    seen_at        TEXT,
    applied_at     TEXT,
    hidden         INTEGER NOT NULL DEFAULT 0,
    starred        INTEGER NOT NULL DEFAULT 0,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_unseen    ON jobs(seen_at, hidden, gone_at);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company   ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_distance  ON jobs(distance_miles);

-- Cache of geocoded location strings so we don't re-hit the geocoder.
CREATE TABLE IF NOT EXISTS geocache (
    query      TEXT PRIMARY KEY,
    lat        REAL,
    lon        REAL,
    resolved   TEXT,
    country    TEXT,               -- ISO2 lowercase, e.g. 'us'; NULL if unresolved
    ok         INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Audit trail of refresh runs, so you can see when a fetcher silently broke.
CREATE TABLE IF NOT EXISTS fetch_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    source      TEXT,
    found       INTEGER DEFAULT 0,
    new_jobs    INTEGER DEFAULT 0,
    errors      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
