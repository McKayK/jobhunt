"""Tests for the v2 pipeline: offline geocoding, settings, parallel refresh,
and query-time filtering. Run: python test_v2.py
"""
import os
import sys
import tempfile
import time

os.environ["JOBHUNT_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import patch

from app import db, geo, refresh, settings, server
from app.fetchers import ats

PASS = 0


def ok(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"[PASS] {name}")


# --- Offline geocoding -------------------------------------------------------
_lindon = geo.local_resolve("84042")
ok("ZIP lookup", _lindon is not None
   and abs(_lindon[0] - 40.34) < 0.05 and abs(_lindon[1] + 111.71) < 0.05)
ok("City, ST", geo.local_resolve("Provo, UT") is not None)
ok("City, full state name", geo.local_resolve("Springville, Utah") is not None)
ok("Country-prefixed", geo.local_resolve("USA - Sandy, UT") is not None)
ok("Country-suffixed", geo.local_resolve("Salt Lake City, Utah, United States") is not None)
ok("Embedded ZIP wins", geo.local_resolve("Lehi, UT 84043") is not None)
ok("No-comma city+ST", geo.local_resolve("Provo UT") is not None)
ok("Garbage is None", geo.local_resolve("5 Locations") is None)
ok("Foreign is None locally", geo.local_resolve("London, United Kingdom") is None)
ok("Remote detection", geo.is_remote("Remote - US") and not geo.is_remote("Lindon, UT"))

t0 = time.time()
for _ in range(2000):
    geo.local_resolve("Draper, UT")
ok("2000 lookups < 0.5s", time.time() - t0 < 0.5)

# --- Settings ----------------------------------------------------------------
conn = db.connect()
db.init_db(conn)

cfg = settings.get_all(conn)
ok("Defaults load", cfg["home_zip"] and cfg["radius_miles"] > 0)

settings.set_many(conn, {"home_zip": "84043", "radius_miles": 15,
                         "include_keywords": ["Engineer", "engineer", "  NURSE "]})
cfg = settings.get_all(conn)
ok("ZIP persists", cfg["home_zip"] == "84043")
ok("Keywords lowercased+deduped", cfg["include_keywords"] == ["engineer", "nurse"])

try:
    settings.set_many(conn, {"home_zip": "abc"})
    ok("Bad ZIP rejected", False)
except ValueError:
    ok("Bad ZIP rejected", True)

settings.set_many(conn, {"home_zip": "84042", "radius_miles": 30,
                         "include_keywords": [], "exclude_keywords": ["intern"]})

# --- Refresh pipeline (mocked network) ---------------------------------------
FAKE = [
    {"company_name": "TestCo", "title": "Software Engineer", "location_raw": "Lindon, UT",
     "url": "https://x/1", "description": None, "posted_at": None, "source": "greenhouse"},
    {"company_name": "TestCo", "title": "Sales Rep", "location_raw": "Denver, CO",
     "url": "https://x/2", "description": None, "posted_at": None, "source": "greenhouse"},
    {"company_name": "TestCo", "title": "Intern - QA", "location_raw": "Orem, UT",
     "url": "https://x/3", "description": None, "posted_at": None, "source": "greenhouse"},
]
AGG = [
    {"company_name": "Local Bakery", "title": "Shift Manager", "location_raw": "Pleasant Grove, UT",
     "url": "https://i/1", "description": None, "posted_at": None, "source": "indeed"},
    {"company_name": "TestCo", "title": "Software Engineer", "location_raw": "Lindon, UT",
     "url": "https://i/2", "description": None, "posted_at": None, "source": "indeed"},
]

db.add_company(conn, "TestCo", "https://x", "greenhouse", "testco")

with patch.object(ats, "fetch_company", lambda *a, **k: list(FAKE)), \
     patch("app.refresh.aggregate.fetch_aggregate",
           lambda **k: (list(AGG), [])):
    totals = refresh.refresh_all(conn, verbose=False)

ok("All postings stored (keywords are query-time)", totals["stored"] == 5)
ok("Cross-source dedupe", conn.execute(
    "SELECT COUNT(*) c FROM jobs WHERE title='Software Engineer'").fetchone()["c"] == 1)
ok("Too-far stored, annotated", conn.execute(
    "SELECT geo_status FROM jobs WHERE title='Sales Rep'").fetchone()["geo_status"] == "too_far")
ok("Aggregate job present", conn.execute(
    "SELECT COUNT(*) c FROM jobs WHERE company_name='Local Bakery'").fetchone()["c"] == 1)
ok("Progress finished", refresh.PROGRESS["running"] is False)

# --- Query-time filtering -----------------------------------------------------
res = server.list_jobs(filter="all")
titles = {j["title"] for j in res["jobs"]}
ok("Exclude keyword hides intern", "Intern - QA" not in titles)
ok("Radius hides Denver", "Sales Rep" not in titles)
ok("Local jobs visible", {"Software Engineer", "Shift Manager"} <= titles)

settings.set_many(conn, {"include_keywords": ["engineer"]})
res = server.list_jobs(filter="all")
titles = {j["title"] for j in res["jobs"]}
ok("Include keyword narrows instantly", titles == {"Software Engineer"})

settings.set_many(conn, {"include_keywords": [], "radius_miles": 500})
res = server.list_jobs(filter="all")
titles = {j["title"] for j in res["jobs"]}
ok("Widening radius reveals stored far jobs", "Sales Rep" in titles)

# Starred/applied tabs ignore discovery filters.
jid = conn.execute("SELECT id FROM jobs WHERE title='Sales Rep'").fetchone()["id"]
conn.execute("UPDATE jobs SET applied_at = datetime('now') WHERE id = ?", (jid,))
conn.commit()
settings.set_many(conn, {"radius_miles": 30, "exclude_keywords": ["sales"]})
res = server.list_jobs(filter="applied")
ok("Applied tab immune to filters",
   any(j["title"] == "Sales Rep" for j in res["jobs"]))

# --- ZIP change recompute ------------------------------------------------------
settings.set_many(conn, {"home_zip": "80202"})   # Denver
n = geo.recompute_all(conn)
d = conn.execute("SELECT distance_miles FROM jobs WHERE title='Sales Rep'").fetchone()[0]
ok("Recompute runs", n >= 3)
ok("Denver job now near new home", d is not None and d < 30)


# --- Auto-discovery ----------------------------------------------------------
from app import autodiscover, detect

settings.set_many(conn, {"home_zip": "84042", "radius_miles": 30,
                         "exclude_keywords": [], "auto_discover": True})

agg_jobs = [
    # In-range, untracked: should become a candidate (2 postings ranks first).
    {"company_name": "Wasatch Widgets Inc", "company_url": "https://wasatchwidgets.com",
     "geo_status": "in_range", "title": "Machinist", "url": "u1"},
    {"company_name": "Wasatch Widgets Inc", "company_url": None,
     "geo_status": "in_range", "title": "Welder", "url": "u2"},
    # In-range but single posting: candidate, ranked second.
    {"company_name": "Provo Pastries", "company_url": None,
     "geo_status": "in_range", "title": "Baker", "url": "u3"},
    # Out of range: never a candidate.
    {"company_name": "Faraway Freight", "company_url": None,
     "geo_status": "too_far", "title": "Driver", "url": "u4"},
    # Nameless: never a candidate.
    {"company_name": "Unknown company", "company_url": None,
     "geo_status": "in_range", "title": "Mystery", "url": "u5"},
]

cands = autodiscover.candidates(conn, agg_jobs, limit=10)
names = [c[0] for c in cands]
ok("Discovery candidates in-range only",
   "Faraway Freight" not in names and "Unknown company" not in names)
ok("Discovery ranks by posting count", names[0] == "Wasatch Widgets Inc")
ok("Discovery carries company_url", cands[0][1] == "https://wasatchwidgets.com")

def fake_detect(name, careers_url=None, probe=True):
    if "Wasatch" in name:
        return detect.Detection("greenhouse", "wasatchwidgets", confidence="medium")
    return detect.Detection("unknown", None, confidence="low")

def fake_fetch(name, ats, slug, row):
    return [{"company_name": name, "title": "Machinist II",
             "location_raw": "Lindon, UT", "url": "https://x/1", "source": ats}]

with patch("app.autodiscover.detect.detect", side_effect=fake_detect), \
     patch("app.autodiscover.ats_fetchers.fetch_company", side_effect=fake_fetch):
    found = autodiscover.run(conn, agg_jobs, limit=5)

ok("Discovery adds confirmed board", len(found) == 1
   and found[0][0]["name"] == "Wasatch Widgets Inc"
   and found[0][0]["ats"] == "greenhouse")
ok("Discovered board fetched immediately",
   found[0][1][0]["title"] == "Machinist II")
row = conn.execute("SELECT * FROM companies WHERE slug='wasatchwidgets'").fetchone()
ok("Discovered company persisted", row is not None and row["active"] == 1)

# Both candidates were attempted; neither should be re-probed next run.
with patch("app.autodiscover.detect.detect", side_effect=AssertionError("re-probed!")):
    found2 = autodiscover.run(conn, agg_jobs, limit=5)
ok("Attempts recorded — no re-probe", found2 == [])

# The miss becomes eligible again after the retry window.
import json as _json
key = autodiscover.META_PREFIX + detect._norm_company("Provo Pastries")
rec = _json.loads(conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()["value"])
ok("Miss recorded as none", rec["status"] == "none")
old = (autodiscover._now() - __import__("datetime").timedelta(days=31)).isoformat()
rec["at"] = old
conn.execute("UPDATE meta SET value=? WHERE key=?", (_json.dumps(rec), key))
conn.commit()
cands = autodiscover.candidates(conn, agg_jobs, limit=10)
ok("Miss retried after window", any(c[0] == "Provo Pastries" for c in cands))
ok("Found company never re-probed",
   not any(c[0] == "Wasatch Widgets Inc" for c in cands))

# Settings round-trip for the new keys.
s = settings.set_many(conn, {"auto_discover": False, "discover_per_refresh": 3})
ok("Discovery settings persist", s["auto_discover"] is False
   and s["discover_per_refresh"] == 3)

print(f"\nALL {PASS} PASSED")
