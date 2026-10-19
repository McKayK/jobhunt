-- Companies you'd actually work for, with their ATS slug once detected.
-- ats = 'link' means "we found their careers page but it's not a board we can
-- read automatically" — kept as a bookmark, never fetched.
CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    careers_url   TEXT,                -- what you pasted in / what we found
    ats           TEXT,                -- greenhouse | lever | ashby | smartrecruiters | workday
                                       -- | workable | recruitee | bamboohr | breezy | link
    slug          TEXT,                -- board identifier for the ATS
    workday_host  TEXT,                -- workday only: e.g. company.wd5.myworkdayjobs.com
    workday_path  TEXT,                -- workday only: e.g. /en-US/External
    board_url     TEXT,                -- public link to the job board itself
    added_by      TEXT,                -- manual | auto | seed
    active        INTEGER NOT NULL DEFAULT 1,
    last_fetch_at TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ats, slug)
);

-- One row per distinct posting. job_hash is stable across sources so the same
-- job seen via Greenhouse and Indeed collapses into one row.
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
    url            TEXT NOT NULL,      -- the posting where we found it
    apply_url      TEXT,               -- employer's own apply page, when known
    company_url    TEXT,               -- employer website, when known
    description    TEXT,
    salary_text    TEXT,
    job_type       TEXT,
    posted_at      TEXT,               -- ISO8601, from the source when available
    source         TEXT NOT NULL,      -- greenhouse | lever | ... | indeed | linkedin | himalayas ...
    first_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    gone_at        TEXT,               -- set when it stops appearing in the feed
    -- Quality signals (recomputed after every refresh).
    risk_score     INTEGER NOT NULL DEFAULT 0,   -- 0..100, higher = more scam-like
    risk_flags     TEXT,                         -- JSON list of human-readable reasons
    is_staffing    INTEGER NOT NULL DEFAULT 0,
    match_score    INTEGER,                      -- 0..100 vs your default resume
    match_detail   TEXT,                         -- JSON {matched:[], missing:[]}
    -- Per-job user state. This is what makes "new" reliable.
    seen_at        TEXT,
    applied_at     TEXT,
    app_status     TEXT,               -- applied | interviewing | offer | rejected | withdrawn
    status_at      TEXT,
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

-- Your resumes (plain text is what matching and tailoring work from).
CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    filename    TEXT,
    text        TEXT NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- AI-tailored resume versions, cached so re-opening one never re-bills.
CREATE TABLE IF NOT EXISTS tailored (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    resume_id   INTEGER REFERENCES resumes(id) ON DELETE SET NULL,
    model       TEXT,
    content     TEXT NOT NULL,     -- JSON
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tailored_job ON tailored(job_id);
