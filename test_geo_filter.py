"""Regression tests for location filtering.

The original bug: geocoding was restricted to countrycodes=us, so foreign
locations returned no result -> distance None -> the "keep it, might be a
match" fallback let every overseas job onto the board. 85 of 148 rows.

Run: python3 test_geo_filter.py
"""
import sqlite3
from app import geo, db, config

# Stubbed geocoder: real strings pulled from the live DB, with country codes.
FAKE = {
    "84042, usa": (40.3416, -111.7208, "us"),
    "lindon, ut": (40.3416, -111.7208, "us"),
    "usa - sandy, ut": (40.5649, -111.8389, "us"),
    "provo, ut": (40.2338, -111.6585, "us"),
    "usa \u2013 santa clara, ca": (37.3541, -121.9552, "us"),
    "salt lake city, utah": (40.7608, -111.8910, "us"),
    "seattle, washington, united states": (47.6062, -122.3321, "us"),
    "new york, new york": (40.7128, -74.0060, "us"),
    "usa - hoboken, nj": (40.7440, -74.0324, "us"),
    "india - pune": (18.5204, 73.8567, "in"),
    "philippines - manila": (14.5995, 120.9842, "ph"),
    "krakow, poland": (50.0647, 19.9450, "pl"),
    "united kingdom - london": (51.5074, -0.1278, "gb"),
    "guangzhou, china": (23.1291, 113.2644, "cn"),
}
geo._http_geocode = lambda q: (
    (lambda v: (v[0], v[1], q, v[2]) if v else None)(FAKE.get(q.strip().lower())))

conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
db.init_db(conn)

CASES = [
    ("Lindon, UT",                          "in_range"),
    ("USA - Sandy, UT",                     "in_range"),
    ("Provo, UT",                           "in_range"),
    ("India - Pune",                        "foreign"),
    ("Philippines - Manila",                "foreign"),
    ("Krakow, Poland",                      "foreign"),
    ("Guangzhou, China",                    "foreign"),
    ("United Kingdom - London",             "foreign"),
    ("Seattle, Washington, United States",  "too_far"),
    ("USA - Hoboken, NJ",                   "too_far"),
    ("Salt Lake City, Utah",                "too_far"),   # 30.3mi, just over a 30 cap
    ("Remote - US",                         "remote"),
    ("Remote",                              "remote"),
    ("Atlantis, Undersea",                  "unknown"),
    # Multi-location: nearest wins. These were being dropped entirely before.
    ("USA - Sandy, UT; USA \u2013 Santa Clara, CA",          "in_range"),
    ("Seattle, Washington, United States; USA - Sandy, UT",  "in_range"),
    ("United Kingdom - London; India - Pune",                "foreign"),
]

fails = 0
print("=== resolve_location ===")
for loc, want in CASES:
    got = geo.resolve_location(conn, loc)["status"]
    ok = got == want
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {loc[:50]:<50} -> {got} (want {want})")

print("\n=== keep/drop policy ===")
from app import refresh
POLICY = [("Lindon, UT", True), ("India - Pune", False),
          ("Seattle, Washington, United States", False), ("Remote", True)]
for loc, want in POLICY:
    keep, _ = refresh.location_matches(conn, {"location_raw": loc})
    ok = keep == want
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {loc[:50]:<50} keep={keep} (want {want})")

print("\n=== multi-location splitting ===")
SPLITS = [
    ("Lindon, UT", ["Lindon, UT"]),                      # comma is NOT a split
    ("A; B", ["A", "B"]),
    ("A | B", ["A", "B"]),
]
for raw, want in SPLITS:
    got = geo.split_locations(raw)
    ok = got == want
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {raw!r} -> {got}")

print("\n" + ("ALL GOOD" if not fails else f"{fails} FAILURES"))
raise SystemExit(1 if fails else 0)
