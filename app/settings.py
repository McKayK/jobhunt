"""Runtime settings, stored in the DB and editable from the UI.

Environment variables seed the defaults on first run; after that the UI owns
them. Changing ZIP or radius takes effect immediately (distances are
recomputed from stored coordinates — no refetch needed). Changing keywords is
purely a query-time filter, so it's instant too.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import config

# key -> (type, default)
DEFAULTS: dict[str, tuple[type, Any]] = {
    "home_zip":         (str,   config.HOME_ZIP),
    "radius_miles":     (float, config.RADIUS_MILES),
    "include_remote":   (bool,  config.INCLUDE_REMOTE),
    "keep_unknown":     (bool,  config.KEEP_UNKNOWN_LOCATIONS),
    # Query-time title filters. Lists of lowercase substrings.
    "include_keywords": (list,  config.TITLE_INCLUDE),
    "exclude_keywords": (list,  config.TITLE_EXCLUDE),
    # Which sources refresh pulls from.
    "use_company_boards": (bool, True),
    "use_indeed":         (bool, True),
    "use_zip_recruiter":  (bool, False),
    "use_google":         (bool, False),
    # How many results to ask each aggregate site for, per search term.
    "results_per_site":   (int,  100),
    # Only pull aggregate postings newer than this many hours (0 = no limit).
    "aggregate_hours_old": (int, 336),   # two weeks
    # Auto-promote nearby companies found by aggregators to tracked ATS boards.
    "auto_discover":        (bool, True),
    "discover_per_refresh": (int,  5),   # probe at most N new companies per refresh
}


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
            val = [str(s).strip().lower() for s in val if str(s).strip()]
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
            import re
            if not re.fullmatch(r"\d{5}", val):
                raise ValueError(f"'{val}' is not a valid 5-digit ZIP code")
        clean[key] = val

    for key, val in clean.items():
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"setting:{key}", _encode(key, val)),
        )
    conn.commit()
    return get_all(conn)
