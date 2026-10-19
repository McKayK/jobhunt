"""Geo helpers: distance math and fast, mostly-offline geocoding.

Resolves ~95% of US locations instantly from an offline index built out of the
`zipcodes` package (every US ZIP with lat/lon, city, state). Nominatim is kept
only as a rate-limited fallback for strings the local index can't place, with
a per-refresh budget so one weird board can't stall a run.

v3 additions:
- Foreign locations ("India - Pune", "London, UK") are recognized offline, so
  they no longer pile up as 'unknown' when the Nominatim budget runs out.
- Bare city names ("Lehi", "Salt Lake City (Hybrid)", "Packsize Salt Lake
  City") resolve against your home state first.
- Remote postings are checked for eligibility: "Remote - EMEA" is foreign,
  "Remote - California" (when you live in Utah) is 'remote_other_state'.
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
    r"\b(remote|work\s*from\s*home|wfh|anywhere|distributed|virtual|telecommute|nationwide)\b", re.I
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
# Work-arrangement decorations: "Salt Lake City (Hybrid)", "Hybrid - Texas".
_ARRANGEMENT = re.compile(
    r"\b(hybrid|on[\s-]?site|in[\s-]?office|in[\s-]?person|office)\b", re.I)
_ZIP_IN_STR = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# Countries and regions we can recognize offline. Value = ISO2 (or a region
# tag). Two-letter codes are deliberately NOT included except 'uk': "IN",
# "CA", "DE" etc. collide with US state abbreviations.
_FOREIGN = {
    "india": "in", "israel": "il", "philippines": "ph", "united kingdom": "gb",
    "uk": "gb", "england": "gb", "scotland": "gb", "wales": "gb", "ireland": "ie",
    "australia": "au", "new zealand": "nz", "spain": "es", "poland": "pl",
    "germany": "de", "france": "fr", "italy": "it", "netherlands": "nl",
    "belgium": "be", "portugal": "pt", "sweden": "se", "norway": "no",
    "denmark": "dk", "finland": "fi", "switzerland": "ch", "austria": "at",
    "czech republic": "cz", "czechia": "cz", "slovakia": "sk", "hungary": "hu",
    "romania": "ro", "bulgaria": "bg", "greece": "gr", "turkey": "tr",
    "ukraine": "ua", "serbia": "rs", "croatia": "hr", "lithuania": "lt",
    "latvia": "lv", "estonia": "ee", "china": "cn", "japan": "jp",
    "south korea": "kr", "korea": "kr", "taiwan": "tw", "hong kong": "hk",
    "singapore": "sg", "malaysia": "my", "indonesia": "id", "thailand": "th",
    "vietnam": "vn", "pakistan": "pk", "bangladesh": "bd", "sri lanka": "lk",
    "united arab emirates": "ae", "uae": "ae", "saudi arabia": "sa",
    "egypt": "eg", "nigeria": "ng", "kenya": "ke", "south africa": "za",
    "brazil": "br", "argentina": "ar", "chile": "cl", "colombia": "co",
    "peru": "pe", "mexico": "mx", "costa rica": "cr", "canada": "ca",
    "emea": "emea", "europe": "eu", "eu": "eu", "apac": "apac", "asia": "apac",
    "latam": "latam", "latin america": "latam", "south america": "latam",
    "africa": "africa", "middle east": "emea",
}
_FOREIGN_CITIES = {   # big foreign cities that show up bare on US-company boards
    "london": "gb", "dublin": "ie", "pune": "in", "bangalore": "in",
    "bengaluru": "in", "hyderabad": "in", "mumbai": "in", "chennai": "in",
    "gurgaon": "in", "noida": "in", "manila": "ph", "toronto": "ca",
    "vancouver": "ca", "montreal": "ca", "sydney": "au", "melbourne": "au",
    "berlin": "de", "munich": "de", "paris": "fr", "amsterdam": "nl",
    "madrid": "es", "barcelona": "es", "krakow": "pl", "warsaw": "pl",
    "bratislava": "sk", "prague": "cz", "tel aviv": "il", "raanana": "il",
    "singapore": "sg", "tokyo": "jp", "seoul": "kr", "shanghai": "cn",
    "beijing": "cn", "guangzhou": "cn", "shenzhen": "cn", "mexico city": "mx",
    "sao paulo": "br", "são paulo": "br", "bogota": "co", "medellin": "co",
    "buenos aires": "ar", "drachten": "nl", "lisbon": "pt", "cork": "ie",
    "belfast": "gb", "edinburgh": "gb", "manchester": "gb", "southampton": "gb",
    "johannesburg": "za", "cape town": "za", "dubai": "ae", "cebu": "ph",
}
_US_MARKERS = re.compile(
    r"\b(us|usa|u\.s\.a?\.?|united states|america|americas|north america|nationwide|"
    r"anywhere|worldwide|global|any location)\b", re.I)


@lru_cache(maxsize=1)
def _index() -> tuple[dict, dict, dict]:
    """Build (zip -> (lat, lon), (city, ST) -> (lat, lon), city -> {states})
    once, lazily. ~43k ZIPs; takes a fraction of a second."""
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
    by_name: dict[str, set] = {}
    for (city, st) in city_ix:
        by_name.setdefault(city, set()).add(st)
    return zip_ix, city_ix, by_name


def _lookup_zip(z: str) -> Optional[Tuple[float, float]]:
    return _index()[0].get(z)


def _lookup_city(city: str, st: str) -> Optional[Tuple[float, float]]:
    return _index()[1].get((city.strip().lower(), st.upper()))


@lru_cache(maxsize=64)
def zip_state(z: str) -> Optional[str]:
    try:
        import zipcodes as zc
        m = zc.matching(z)
        if m:
            return m[0]["state"]
    except Exception:
        pass
    return None


def _clean_part(p: str) -> str:
    p = _METRO_NOISE.sub(" ", p)
    p = _ARRANGEMENT.sub(" ", p)
    return re.sub(r"\s+", " ", p).strip(" -–—.")


def foreign_country(place: Optional[str]) -> Optional[str]:
    """Offline: does this string name a non-US country/region/big city?"""
    if not place:
        return None
    low = place.lower()
    # "New Mexico" / "New Jersey" must not read as Mexico / a foreign place.
    for full in _STATE_ABBR:
        if " " in full:
            low = re.sub(rf"\b{full}\b", " ", low)
    tokens = [t.strip(" .-()") for t in re.split(r"[,;/|()–—]|\s-\s", low)]
    tokens = [t for t in tokens if t]
    for t in tokens:
        if t in _FOREIGN:
            return _FOREIGN[t]
        if t in _FOREIGN_CITIES:
            return _FOREIGN_CITIES[t]
        # "india - pune" style handled by the split; "pune india" without punctuation:
        for name, code in _FOREIGN.items():
            if len(name) > 3 and re.search(rf"\b{re.escape(name)}\b", t):
                return code
    # Trailing "my"/"uk"-style country codes after a comma.
    m = re.search(r",\s*(uk|gb|my|sg|ph|au|nz|ie|nl|fr|es|pl|il)\s*$", low)
    if m:
        return m.group(1)
    return None


def local_resolve(place: str, home_state: Optional[str] = None) -> Optional[Tuple[float, float]]:
    """Resolve one US place string offline. Returns (lat, lon) or None.

    Handles: '84042', 'Lindon, UT', 'Lindon, Utah', 'Lehi, UT 84043',
    'USA - Sandy, UT', 'Salt Lake City, Utah, United States',
    'Greater Salt Lake City Area', 'Salt Lake City (Hybrid)', and — with
    home_state — bare 'Lehi' or 'Packsize Salt Lake City'.
    """
    if not place or not place.strip():
        return None
    s = place.strip()

    m = _ZIP_IN_STR.search(s)
    if m:
        hit = _lookup_zip(m.group(1))
        if hit:
            return hit

    # Parenthetical content is often the city: "Hybrid - Texas (Westlake)".
    paren = re.findall(r"\(([^)]*)\)", s)
    base = re.sub(r"\([^)]*\)", " ", s)
    # A full "City, ST" in parentheses wins: 'Weave - Headquarters (Lehi, UT)'.
    for p in paren:
        if "," in p:
            hit = local_resolve(p, home_state)
            if hit:
                return hit

    parts = [_clean_part(p) for p in re.split(r"[,•;|/]", base)]
    parts = [p for p in parts if p and p.lower() not in _COUNTRY_TAILS]

    # Walk adjacent (city, state) pairs: ['USA - Sandy', 'UT'] and
    # ['Salt Lake City', 'Utah', 'United States'] both land here.
    for i in range(len(parts) - 1):
        city = re.sub(r"^(usa?|united states)\s*[-–]\s*", "", parts[i], flags=re.I)
        nxt = parts[i + 1].lower()
        st = nxt.upper() if nxt.upper() in _VALID_ABBRS else _STATE_ABBR.get(nxt)
        if st:
            hit = _lookup_city(city, st)
            if hit:
                return hit

    # "State - City" or "City - State": 'Texas - Westlake', 'Hybrid - Texas (Westlake)'.
    dash = [_clean_part(p) for p in re.split(r"\s[-–—]\s", base)]
    dash = [p for p in dash if p and p.lower() not in _COUNTRY_TAILS]
    states_in = [_STATE_ABBR.get(p.lower()) or (p.upper() if p.upper() in _VALID_ABBRS else None)
                 for p in dash]
    for st in [x for x in states_in if x]:
        for cand in dash + paren:
            hit = _lookup_city(_clean_part(cand), st)
            if hit:
                return hit

    if not parts:
        parts = [_clean_part(p) for p in paren if _clean_part(p)]
        if not parts:
            return None

    # Single token: 'Lindon UT' (no comma).
    single = parts[0]
    m = re.fullmatch(r"(.+?)\s+([A-Za-z]{2})", single)
    if m and m.group(2).upper() in _VALID_ABBRS:
        hit = _lookup_city(m.group(1), m.group(2).upper())
        if hit:
            return hit

    # Bare city name ('Lehi'), or a city at the end of a label
    # ('Packsize Salt Lake City'): your home state first, then a city name
    # that exists in only one state.
    if len(parts) == 1:
        words = single.split()
        for n in range(min(4, len(words)), 0, -1):
            cand = " ".join(words[-n:]).lower()
            if home_state:
                hit = _lookup_city(cand, home_state)
                if hit:
                    return hit
            if n == len(words):
                states = _index()[2].get(cand)
                if states and len(states) == 1:
                    return _lookup_city(cand, next(iter(states)))
    return None


def remote_eligibility(location_raw: Optional[str], home_state: Optional[str]) -> str:
    """For a remote posting: 'us' (open to you), 'foreign' (another country /
    region only), or 'other_state' (US but restricted to states you're not in).
    """
    if not location_raw:
        return "us"
    low = location_raw.lower()
    if _US_MARKERS.search(low):
        return "us"
    fc = foreign_country(low)
    if fc:
        return "foreign"
    # State restrictions: "Remote - California", "Remote (CA, NY, TX)".
    named = set()
    for full, ab in _STATE_ABBR.items():
        if re.search(rf"\b{full}\b", low):
            named.add(ab)
    for ab in re.findall(r"\b([A-Z]{2})\b", location_raw):
        if ab in _VALID_ABBRS:
            named.add(ab)
    if named and home_state and home_state not in named:
        return "other_state"
    return "us"


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
            budget: Optional[GeoBudget] = None,
            home_state: Optional[str] = None) -> Optional[Tuple[float, float, str]]:
    """Geocode a free-text location: local index -> offline foreign check ->
    cache -> Nominatim. Returns (lat, lon, country_code) or None. Foreign
    places recognized offline come back with lat/lon None."""
    if not query or not query.strip():
        return None
    key = query.strip().lower()

    hit = local_resolve(query, home_state)
    if hit:
        return hit[0], hit[1], "us"

    fc = foreign_country(query)
    if fc:
        return None, None, fc

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
    if net is None or net[0] is None:
        raise RuntimeError(f"Could not locate ZIP {z}. Double-check it in Settings.")
    return net[0], net[1]


def home_state(conn: sqlite3.Connection) -> Optional[str]:
    return zip_state(settings.get(conn, "home_zip"))


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
    parts = [p.strip(" -–—\t") for p in _SPLIT_PAT.split(location_raw)]
    return [p for p in parts if p]


def resolve_location(conn: sqlite3.Connection, location_raw: Optional[str],
                     home: Optional[Tuple[float, float]] = None,
                     radius: Optional[float] = None,
                     budget: Optional[GeoBudget] = None,
                     home_st: Optional[str] = None,
                     remote_hint: bool = False) -> dict:
    """Resolve a posting's location string to the best (nearest) match.

    Returns dict with:
      status: 'remote' | 'remote_other_state' | 'in_range' | 'too_far' |
              'foreign' | 'unknown'
      distance_miles, lat, lon, country
    `remote_hint` is set when the source itself says the job is remote.
    """
    out = {"status": "unknown", "distance_miles": None,
           "lat": None, "lon": None, "country": None}

    if home_st is None:
        home_st = home_state(conn)

    if remote_hint or is_remote(location_raw):
        elig = remote_eligibility(location_raw, home_st)
        if elig == "foreign":
            out["status"] = "foreign"
            out["country"] = foreign_country(location_raw)
        elif elig == "other_state":
            out["status"] = "remote_other_state"
            out["country"] = "us"
        else:
            out["status"] = "remote"
            out["country"] = "us"
        return out

    if not location_raw:
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
    saw_foreign = None

    for cand in candidates:
        hit = geocode(conn, cand, budget=budget, home_state=home_st)
        if hit is None:
            continue
        lat, lon, country = hit
        if country and country != "us":
            saw_foreign = saw_foreign or country
            continue
        if lat is None:
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
        out["country"] = saw_foreign
    return out


def recompute_all(conn: sqlite3.Connection) -> int:
    """Re-derive distance and in/out-of-range status for every stored job.

    Called when the ZIP or radius changes (and once after upgrading, to fix
    rows the older resolver left 'unknown') — pure math on stored coordinates
    plus offline lookups, so it's quick.
    """
    home = home_coords(conn)
    radius = settings.get(conn, "radius_miles")
    hst = home_state(conn)
    hlat, hlon = home
    n = 0

    rows = conn.execute(
        "SELECT id, location_raw, lat, lon, geo_status, remote FROM jobs"
    ).fetchall()
    for r in rows:
        status = r["geo_status"]
        if status in ("remote", "remote_other_state") or r["remote"]:
            elig = remote_eligibility(r["location_raw"], hst)
            new = {"foreign": "foreign", "other_state": "remote_other_state"}.get(elig, "remote")
            if new != status:
                conn.execute("UPDATE jobs SET geo_status=? WHERE id=?", (new, r["id"]))
                n += 1
            continue
        if status == "foreign":
            continue
        lat, lon = r["lat"], r["lon"]
        if lat is None or lon is None:
            # Another offline attempt for 'unknown' rows.
            raw = r["location_raw"] or ""
            hit = None
            for cand in split_locations(raw) or [raw]:
                hit = local_resolve(cand, hst)
                if hit:
                    break
            if not hit:
                fc = foreign_country(raw)
                if fc and status != "foreign":
                    conn.execute("UPDATE jobs SET geo_status='foreign', country=? WHERE id=?",
                                 (fc, r["id"]))
                    n += 1
                continue
            lat, lon = hit
        d = haversine_miles(hlat, hlon, lat, lon)
        status = "in_range" if d <= radius else "too_far"
        conn.execute(
            "UPDATE jobs SET lat=?, lon=?, distance_miles=?, geo_status=?, country='us' WHERE id=?",
            (lat, lon, d, status, r["id"]),
        )
        n += 1
    conn.commit()
    return n
