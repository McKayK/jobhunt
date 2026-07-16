"""Geo helpers: distance math and fast, mostly-offline geocoding.

The old version hit Nominatim (1.1 s per lookup, by their policy) for every
new location string, which made the first refresh crawl. This version resolves
~95% of US locations instantly from an offline index built out of the
`zipcodes` package (every US ZIP with lat/lon, city, state). Nominatim is kept
only as a rate-limited fallback for strings the local index can't place, with
a per-refresh budget so one weird board can't stall a run.
"""
from __future__ import annotations

import math
import re
import time
import sqlite3
from functools import lru_cache
from typing import Optional, Tuple

import requests

from . import config, settings

_last_geocode_call = 0.0

# Location strings that mean "not a physical place near you".
_REMOTE_PAT = re.compile(
    r"\b(remote|work\s*from\s*home|wfh|anywhere|distributed|virtual|telecommute)\b", re.I
)


def is_remote(location_raw: Optional[str]) -> bool:
    return bool(location_raw and _REMOTE_PAT.search(location_raw))


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --- Offline US index -------------------------------------------------------

_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}
_VALID_ABBRS = set(_STATE_ABBR.values())

_COUNTRY_TAILS = {"us", "usa", "u.s.", "u.s.a.", "united states",
                  "united states of america", "america"}
# Words that decorate metro-area strings but aren't part of the city name.
_METRO_NOISE = re.compile(
    r"\b(greater|metro(politan)?( area)?|area|region|county)\b", re.I
)
_ZIP_IN_STR = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


@lru_cache(maxsize=1)
def _index() -> tuple[dict, dict]:
    """Build (zip -> (lat, lon), (city, ST) -> (lat, lon)) once, lazily.

    ~43k ZIPs; takes a fraction of a second and lives for the process.
    City centroids are the mean of that city's ZIP coordinates.
    """
    import zipcodes as zc

    zip_ix: dict[str, tuple[float, float]] = {}
    acc: dict[tuple[str, str], list[tuple[float, float]]] = {}

    for e in zc.list_all():
        try:
            lat, lon = float(e["lat"]), float(e["long"])
        except (TypeError, ValueError, KeyError):
            continue
        z = e.get("zip_code")
        if z:
            zip_ix[z] = (lat, lon)
        city, st = (e.get("city") or "").strip().lower(), e.get("state") or ""
        if city and st in _VALID_ABBRS:
            acc.setdefault((city, st), []).append((lat, lon))
            for alt in e.get("acceptable_cities") or []:
                alt = (alt or "").strip().lower()
                if alt:
                    acc.setdefault((alt, st), []).append((lat, lon))

    city_ix = {
        k: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        for k, pts in acc.items()
    }
    return zip_ix, city_ix


def _lookup_zip(z: str) -> Optional[Tuple[float, float]]:
    return _index()[0].get(z)


def _lookup_city(city: str, st: str) -> Optional[Tuple[float, float]]:
    return _index()[1].get((city.strip().lower(), st.upper()))


def _clean_part(p: str) -> str:
    p = _METRO_NOISE.sub(" ", p)
    return re.sub(r"\s+", " ", p).strip(" -\u2013\u2014.")


def local_resolve(place: str) -> Optional[Tuple[float, float]]:
    """Resolve one US place string offline. Returns (lat, lon) or None.

    Handles: '84042', 'Lindon, UT', 'Lindon, Utah', 'Lehi, UT 84043',
    'USA - Sandy, UT', 'Salt Lake City, Utah, United States',
    'Greater Salt Lake City Area'.
    """
    if not place or not place.strip():
        return None
    s = place.strip()

    m = _ZIP_IN_STR.search(s)
    if m:
        hit = _lookup_zip(m.group(1))
        if hit:
            return hit

    parts = [_clean_part(p) for p in re.split(r"[,\u2022;|/]", s)]
    parts = [p for p in parts if p and p.lower() not in _COUNTRY_TAILS]
    if not parts:
        return None

    # Walk adjacent (city, state) pairs: ['USA - Sandy', 'UT'] and
    # ['Salt Lake City', 'Utah', 'United States'] both land here.
    for i in range(len(parts) - 1):
        city = re.sub(r"^(usa?|united states)\s*[-\u2013]\s*", "", parts[i], flags=re.I)
        nxt = parts[i + 1].lower()
        st = nxt.upper() if nxt.upper() in _VALID_ABBRS else _STATE_ABBR.get(nxt)
        if st:
            hit = _lookup_city(city, st)
            if hit:
                return hit

    # Single token: 'Lindon UT' (no comma) or a bare well-known city.
    single = parts[0]
    m = re.fullmatch(r"(.+?)\s+([A-Za-z]{2})", single)
    if m and m.group(2).upper() in _VALID_ABBRS:
        hit = _lookup_city(m.group(1), m.group(2).upper())
        if hit:
            return hit
    return None


# --- Nominatim fallback (rate-limited, budgeted) ----------------------------

def _http_geocode(query: str) -> Optional[Tuple[float, float, str, str]]:
    """Hit Nominatim, respecting its 1 req/sec policy."""
    global _last_geocode_call
    elapsed = time.time() - _last_geocode_call
    if elapsed < config.GEOCODE_DELAY:
        time.sleep(config.GEOCODE_DELAY - elapsed)
    _last_geocode_call = time.time()

    try:
        resp = requests.get(
            config.NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        top = results[0]
        cc = (top.get("address", {}) or {}).get("country_code", "") or ""
        return (float(top["lat"]), float(top["lon"]),
                top.get("display_name", query), cc.lower())
    except Exception:
        return None


class GeoBudget:
    """Caps slow network geocodes per refresh so one messy board can't stall it.

    Locations that miss the budget stay 'unknown' this run and get another
    chance next refresh (the cache remembers what already failed for real).
    """
    def __init__(self, n: int = 40):
        self.remaining = n

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def geocode(conn: sqlite3.Connection, query: str,
            budget: Optional[GeoBudget] = None) -> Optional[Tuple[float, float, str]]:
    """Geocode a free-text location: local index -> cache -> Nominatim.

    Returns (lat, lon, country_code) or None.
    """
    if not query or not query.strip():
        return None
    key = query.strip().lower()

    hit = local_resolve(query)
    if hit:
        return hit[0], hit[1], "us"

    row = conn.execute(
        "SELECT lat, lon, country, ok FROM geocache WHERE query = ?", (key,)
    ).fetchone()
    if row is not None:
        if not row["ok"]:
            return None
        return row["lat"], row["lon"], (row["country"] or "")

    if budget is not None and not budget.take():
        return None   # not cached as a miss — retried next run

    q = f"{query.strip()}, USA" if re.fullmatch(r"\d{5}", query.strip()) else query.strip()
    net = _http_geocode(q)
    if net is None:
        conn.execute(
            "INSERT OR REPLACE INTO geocache (query, lat, lon, resolved, country, ok) "
            "VALUES (?,?,?,?,?,0)",
            (key, None, None, None, None),
        )
        conn.commit()
        return None

    lat, lon, resolved, country = net
    conn.execute(
        "INSERT OR REPLACE INTO geocache (query, lat, lon, resolved, country, ok) "
        "VALUES (?,?,?,?,?,1)",
        (key, lat, lon, resolved, country),
    )
    conn.commit()
    return lat, lon, country


# --- Home ------------------------------------------------------------------

def home_coords(conn: sqlite3.Connection) -> Tuple[float, float]:
    """Coordinates for the ZIP currently set in settings."""
    z = settings.get(conn, "home_zip")
    hit = _lookup_zip(z)
    if hit:
        return hit
    net = geocode(conn, z)
    if net is None:
        raise RuntimeError(f"Could not locate ZIP {z}. Double-check it in Settings.")
    return net[0], net[1]


def zip_place_name(z: str) -> Optional[str]:
    """'84042' -> 'Lindon, UT' for friendlier UI labels."""
    try:
        import zipcodes as zc
        m = zc.matching(z)
        if m:
            return f"{m[0]['city']}, {m[0]['state']}"
    except Exception:
        pass
    return None


_SPLIT_PAT = re.compile(r"\s*(?:;|\||\bor\b|/)\s*")


def split_locations(location_raw: str) -> list[str]:
    """'USA - Sandy, UT; USA - Santa Clara, CA' -> both parts.
    Commas are NOT split on — they separate city from state."""
    if not location_raw:
        return []
    parts = [p.strip(" -\u2013\u2014\t") for p in _SPLIT_PAT.split(location_raw)]
    return [p for p in parts if p]


def resolve_location(conn: sqlite3.Connection, location_raw: Optional[str],
                     home: Optional[Tuple[float, float]] = None,
                     radius: Optional[float] = None,
                     budget: Optional[GeoBudget] = None) -> dict:
    """Resolve a posting's location string to the best (nearest) match.

    Returns dict with:
      status: 'remote' | 'in_range' | 'too_far' | 'foreign' | 'unknown'
      distance_miles, lat, lon, country
    """
    out = {"status": "unknown", "distance_miles": None,
           "lat": None, "lon": None, "country": None}

    if not location_raw:
        return out
    if is_remote(location_raw):
        out["status"] = "remote"
        return out

    candidates = split_locations(location_raw)
    if not candidates:
        return out

    if home is None:
        home = home_coords(conn)
    if radius is None:
        radius = settings.get(conn, "radius_miles")
    hlat, hlon = home

    best = None
    saw_foreign = False

    for cand in candidates:
        hit = geocode(conn, cand, budget=budget)
        if hit is None:
            continue
        lat, lon, country = hit
        if country and country != "us":
            saw_foreign = True
            if best is None:
                out["country"] = country
                out["lat"], out["lon"] = lat, lon
            continue
        d = haversine_miles(hlat, hlon, lat, lon)
        if best is None or d < best[0]:
            best = (d, lat, lon, country or "us")

    if best is not None:
        d, lat, lon, country = best
        out.update(distance_miles=d, lat=lat, lon=lon, country=country)
        out["status"] = "in_range" if d <= radius else "too_far"
        return out

    if saw_foreign:
        out["status"] = "foreign"
        return out
    return out


def recompute_all(conn: sqlite3.Connection) -> int:
    """Re-derive distance and in/out-of-range status for every stored job.

    Called when the ZIP or radius changes — pure math on stored coordinates
    plus offline lookups for previously-unknown locations, so it's instant.
    """
    home = home_coords(conn)
    radius = settings.get(conn, "radius_miles")
    hlat, hlon = home
    n = 0

    rows = conn.execute(
        "SELECT id, location_raw, lat, lon, geo_status FROM jobs"
    ).fetchall()
    for r in rows:
        if r["geo_status"] in ("remote", "foreign"):
            continue
        lat, lon = r["lat"], r["lon"]
        if lat is None or lon is None:
            # One more offline attempt for old 'unknown' rows.
            hit = local_resolve(r["location_raw"] or "")
            if not hit:
                continue
            lat, lon = hit
        d = haversine_miles(hlat, hlon, lat, lon)
        status = "in_range" if d <= radius else "too_far"
        conn.execute(
            "UPDATE jobs SET lat=?, lon=?, distance_miles=?, geo_status=? WHERE id=?",
            (lat, lon, d, status, r["id"]),
        )
        n += 1
    conn.commit()
    return n


def distance_from_home(conn: sqlite3.Connection, location_raw: Optional[str]
                       ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Back-compat shim: (distance_miles, lat, lon)."""
    r = resolve_location(conn, location_raw)
    return r["distance_miles"], r["lat"], r["lon"]
