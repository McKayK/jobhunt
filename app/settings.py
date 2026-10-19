"""Runtime settings, stored in the DB and editable from the UI.

Environment variables seed the defaults on first run; after that the UI owns
them. Changing ZIP or radius takes effect immediately (distances are
recomputed from stored coordinates — no refetch needed). Changing keywords is
purely a query-time filter, so it's instant too.

API keys pasted into the UI are stored under 'secret:' keys and are never
returned by the API — only whether one is set.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import config

# key -> (type, default)
DEFAULTS: dict[str, tuple[type, Any]] = {
    "home_zip":         (str,   config.HOME_ZIP),
    "radius_miles":     (float, config.RADIUS_MILES),
    "include_remote":   (bool,  config.INCLUDE_REMOTE),
    "keep_unknown":     (bool,  config.KEEP_UNKNOWN_LOCATIONS),
    # Query-time title filters. Lists of lowercase words/phrases, matched on
    # word boundaries ("intern" no longer hides "Internal Tools Engineer").
    "include_keywords": (list,  config.TITLE_INCLUDE),
    "exclude_keywords": (list,  config.TITLE_EXCLUDE),
    # Which sources refresh pulls from.
    "use_company_boards": (bool, True),
    "use_indeed":         (bool, True),
    "use_zip_recruiter":  (bool, False),
    "use_google":         (bool, False),
    "use_linkedin":       (bool, False),
    "use_glassdoor":      (bool, False),
    # Search for remote jobs open to anyone in the US (not just near you):
    # job sites searched with "United States" + remote, plus remote-only boards.
    "remote_nationwide":  (bool, True),
    "use_remote_boards":  (bool, True),
    # How many results to ask each job site for, per search term.
    "results_per_site":   (int,  100),
    # Only pull job-site postings newer than this many hours (0 = no limit).
    "aggregate_hours_old": (int, 336),   # two weeks
    # Job-site postings no search has returned for this many days are expired.
    "aggregate_expire_days": (int, 10),
    # Auto-promote companies found on job sites to tracked boards.
    "auto_discover":        (bool, True),
    "discover_per_refresh": (int,  10),
    # Scam / spam filtering.
    "hide_risky":       (bool, True),
    "risk_threshold":   (int,  60),      # risk_score >= this counts as risky
    "hide_staffing":    (bool, False),
    "blocked_companies": (list, []),
    # AI provider for resume tailoring: auto | anthropic | gemini
    "ai_provider":      (str,  "auto"),
}

SECRET_KEYS = ("anthropic_api_key", "gemini_api_key")


def _coerce(key: str, raw: str) -> Any:
    typ, default = DEFAULTS[key]
    try:
        if typ is bool:
            return raw == "1"
        if typ is list:
            v = json.loads(raw)
            return v if isinstance(v, list) else default
        return typ(raw)
    except Exception:
        return default


def _encode(key: str, value: Any) -> str:
    typ, _ = DEFAULTS[key]
    if typ is bool:
        return "1" if value else "0"
    if typ is list:
        return json.dumps(value)
    return str(value)


def get_all(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'setting:%'"
    ).fetchall()
    stored = {r["key"][len("setting:"):]: r["value"] for r in rows}
    out = {}
    for key, (_typ, default) in DEFAULTS.items():
        out[key] = _coerce(key, stored[key]) if key in stored else default
    return out


def get(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (f"setting:{key}",)
    ).fetchone()
    if row is None:
        return DEFAULTS[key][1]
    return _coerce(key, row["value"])


def set_many(conn: sqlite3.Connection, values: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist a partial settings update. Returns the full set."""
    clean: dict[str, Any] = {}
    for key, val in values.items():
        if key not in DEFAULTS:
            continue
        typ, default = DEFAULTS[key]
        if typ is list:
            if not isinstance(val, list):
                continue
            val = [re.sub(r"\s+", " ", str(s)).strip().lower() for s in val if str(s).strip()]
            # De-dupe, keep order.
            seen = set()
            val = [s for s in val if not (s in seen or seen.add(s))]
        elif typ is bool:
            val = bool(val)
        elif typ is float:
            try:
                val = max(1.0, min(500.0, float(val)))
            except (TypeError, ValueError):
                continue
        elif typ is int:
            try:
                val = max(0, min(1000, int(val)))
            except (TypeError, ValueError):
                continue
        elif key == "home_zip":
            val = str(val).strip()
            if not re.fullmatch(r"\d{5}", val):
                raise ValueError(f"'{val}' is not a valid 5-digit ZIP code")
        elif key == "ai_provider":
            val = str(val).strip().lower()
            if val not in ("auto", "anthropic", "gemini"):
                continue
        clean[key] = val

    for key, val in clean.items():
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"setting:{key}", _encode(key, val)),
        )
    conn.commit()
    return get_all(conn)


# --- Secrets (API keys) ------------------------------------------------------

def get_secret(conn: sqlite3.Connection, key: str) -> str:
    """Environment variable first, then a key pasted into the UI."""
    env = {"anthropic_api_key": config.ANTHROPIC_API_KEY,
           "gemini_api_key": config.GEMINI_API_KEY}.get(key, "")
    if env:
        return env
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"secret:{key}",)).fetchone()
    return (row["value"] if row else "") or ""


def set_secret(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in SECRET_KEYS:
        return
    value = (value or "").strip()
    if value:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (f"secret:{key}", value))
    else:
        conn.execute("DELETE FROM meta WHERE key = ?", (f"secret:{key}",))
    conn.commit()


def secrets_status(conn: sqlite3.Connection) -> dict[str, Any]:
    out = {}
    for k in SECRET_KEYS:
        env = {"anthropic_api_key": config.ANTHROPIC_API_KEY,
               "gemini_api_key": config.GEMINI_API_KEY}[k]
        out[k] = {"set": bool(get_secret(conn, k)), "from_env": bool(env)}
    return out
