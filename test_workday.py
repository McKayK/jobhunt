"""Workday fetcher tests against a fixture shaped like the real CXS response.

SAFETY: no DB writes here, but force a temp data dir anyway so an accidental
db.connect() can never touch a real board.
"""
import os, sys, tempfile
os.environ["JOBHUNT_DATA_DIR"] = tempfile.mkdtemp(prefix="jobhunt-test-")

from unittest import mock
from app.fetchers import ats

fails = 0
def check(cond, label):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")

# --- site id extraction ----------------------------------------------------
print("=== _workday_site_id ===")
for path, want in [
    ("/en-US/External", "External"),
    ("/External", "External"),
    ("/en-US/adobecareers", "adobecareers"),
    ("/fr-FR/Ancestry_Careers", "Ancestry_Careers"),
    ("/", None),
    (None, None),
]:
    got = ats._workday_site_id(path)
    check(got == want, f"{path!r} -> {got!r} (want {want!r})")

# --- posted_on parsing -----------------------------------------------------
print("\n=== _workday_posted (relative dates) ===")
from datetime import datetime, timezone, timedelta
now = datetime.now(timezone.utc)
def days_ago(s):
    v = ats._workday_posted(s)
    if not v: return None
    return round((now - datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds() / 86400)
check(days_ago("Posted Today") == 0, "'Posted Today' -> ~0 days")
check(days_ago("Posted Yesterday") == 1, "'Posted Yesterday' -> ~1 day")
check(days_ago("Posted 3 Days Ago") == 3, "'Posted 3 Days Ago' -> 3 days")
check(days_ago("Posted 30+ Days Ago") == 30, "'Posted 30+ Days Ago' -> 30 days")
check(days_ago("Posted 2 Weeks Ago") == 14, "'Posted 2 Weeks Ago' -> 14 days")
check(ats._workday_posted(None) is None, "None -> None")
check(ats._workday_posted("garbage") is None, "unparseable -> None")

# --- fetch + pagination ----------------------------------------------------
print("\n=== fetch_workday ===")
PAGE1 = {"total": 3, "jobPostings": [
    {"title": "Software Engineer", "externalPath": "/job/Lindon-UT/Software-Engineer_R123",
     "locationsText": "Lindon, UT", "postedOn": "Posted 3 Days Ago"},
    {"title": "Data Analyst", "externalPath": "/job/Provo-UT/Data-Analyst_R124",
     "locationsText": "Provo, UT", "postedOn": "Posted Today"},
]}
PAGE2 = {"total": 3, "jobPostings": [
    {"title": "PM", "externalPath": "/job/Pune/PM_R125",
     "locationsText": "India - Pune", "postedOn": "Posted 30+ Days Ago"},
]}

calls = []
def fake_post(url, **kw):
    calls.append((url, kw["json"]["offset"]))
    m = mock.Mock(); m.status_code = 200
    m.json.return_value = PAGE1 if kw["json"]["offset"] == 0 else PAGE2
    m.raise_for_status.return_value = None
    return m

with mock.patch.object(ats.requests, "post", side_effect=fake_post):
    jobs = ats.fetch_workday("Adobe", "adobe", "adobe.wd5.myworkdayjobs.com", "/en-US/External")

check(len(jobs) == 3, f"paginates to all 3 postings (got {len(jobs)})")
check(calls[0][0] == "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/External/jobs",
      f"correct CXS endpoint ({calls[0][0]})")
check([c[1] for c in calls] == [0, 2], f"offsets advance by page size (got {[c[1] for c in calls]})")
check(jobs[0]["url"] == "https://adobe.wd5.myworkdayjobs.com/job/Lindon-UT/Software-Engineer_R123",
      "externalPath joined to absolute URL")
check(jobs[0]["location_raw"] == "Lindon, UT", "location_raw preserved verbatim for geo.py")
check(jobs[0]["source"] == "workday", "source tagged workday")
check(all(set(j) >= {"company_name","title","location_raw","url","description","posted_at","source"}
          for j in jobs), "matches the shared fetcher contract")

# --- failure modes ---------------------------------------------------------
print("\n=== error handling ===")
try:
    ats.fetch_workday("X", "x", None, "/en-US/External"); check(False, "missing host raises")
except ValueError: check(True, "missing host raises ValueError")
try:
    ats.fetch_workday("X", "x", "x.wd1.myworkdayjobs.com", None); check(False, "bad path raises")
except ValueError: check(True, "underivable site id raises ValueError")

def empty_post(url, **kw):
    m = mock.Mock(); m.status_code = 200
    m.json.return_value = {"total": 0, "jobPostings": []}
    m.raise_for_status.return_value = None
    return m
with mock.patch.object(ats.requests, "post", side_effect=empty_post):
    check(ats.fetch_workday("X","x","x.wd1.myworkdayjobs.com","/en-US/External") == [],
          "empty board returns [] without looping")

# --- registry --------------------------------------------------------------
print("\n=== registry wiring ===")
check("workday" in ats.FETCHERS, "workday registered in FETCHERS")
row = {"workday_host": "adobe.wd5.myworkdayjobs.com", "workday_path": "/en-US/External"}
with mock.patch.object(ats.requests, "post", side_effect=fake_post):
    jobs = ats.fetch_company("Adobe", "workday", "adobe", row)
check(len(jobs) == 3, "fetch_company routes workday with host/path from row")
with mock.patch.object(ats.requests, "post", side_effect=fake_post):
    try:
        ats.fetch_company("Adobe", "workday", "adobe")
        check(False, "no row -> should raise")
    except ValueError:
        check(True, "fetch_company without row raises rather than silently empty")


# --- CLI add path ----------------------------------------------------------
# Regression: SUPPORTED was a hardcoded tuple missing 'workday', so every
# Workday company was skipped at add time and the fetcher never ran. Deriving
# it from FETCHERS makes that class of drift impossible.
print("\n=== cli add path ===")
from app import cli, db, detect as detect_mod

check("workday" in cli.SUPPORTED, "SUPPORTED includes workday")
check(set(cli.SUPPORTED) == set(ats.FETCHERS), "SUPPORTED derived from FETCHERS (can't drift)")

conn = db.connect(); db.init_db(conn)
det = detect_mod.Detection("workday", "adobe", "adobe.wd5.myworkdayjobs.com",
                           "/en-US/External", "high", "")
with mock.patch.object(detect_mod, "detect", return_value=det):
    added = cli._add_one(conn, "Adobe", "https://adobe.wd5.myworkdayjobs.com/en-US/External", quiet=True)
check(added, "workday company is added, not skipped")
r = conn.execute("SELECT workday_host, workday_path FROM companies WHERE name='Adobe'").fetchone()
check(bool(r) and r["workday_host"] == "adobe.wd5.myworkdayjobs.com", "workday_host persisted")
check(bool(r) and r["workday_path"] == "/en-US/External", "workday_path persisted")

det_nopath = detect_mod.Detection("workday", "x", "x.wd1.myworkdayjobs.com", None, "medium", "")
with mock.patch.object(detect_mod, "detect", return_value=det_nopath):
    added = cli._add_one(conn, "NoPath", "https://x.com/careers", quiet=True)
check(not added, "workday without site path is refused, not stored broken")

print("\n" + ("ALL GOOD" if not fails else f"{fails} FAILURES"))
raise SystemExit(1 if fails else 0)
