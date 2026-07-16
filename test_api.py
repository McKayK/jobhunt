"""Exercise the HTTP API end-to-end against a seeded DB."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["JOBHUNT_DATA_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_test")

from fastapi.testclient import TestClient
from app import db, config
from app.server import app

# Fresh DB
import shutil, pathlib
shutil.rmtree(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_test"), ignore_errors=True)
pathlib.Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_test")).mkdir(parents=True, exist_ok=True)

conn = db.connect()
db.init_db(conn)
cid = db.add_company(conn, "Acme Corp", "https://acme.com/careers", "greenhouse", "acme")

seed = [
    dict(company_name="Acme Corp", title="Software Engineer", location_raw="Lindon, UT",
         url="https://x/1", source="greenhouse", distance_miles=2.0, remote=False,
         posted_at="2026-07-13 09:00:00", company_id=cid),
    dict(company_name="Acme Corp", title="Data Analyst", location_raw="Remote - US",
         url="https://x/2", source="greenhouse", distance_miles=None, remote=True,
         posted_at="2026-07-12 09:00:00", company_id=cid),
    dict(company_name="Globex", title="DevOps Engineer", location_raw="Salt Lake City, UT",
         url="https://x/3", source="lever", distance_miles=28.0, remote=False,
         posted_at="2026-07-11 09:00:00"),
    dict(company_name="Initech", title="QA Engineer", location_raw="Boise, ID",
         url="https://x/4", source="lever", distance_miles=290.0, remote=False,
         posted_at="2026-07-10 09:00:00"),
    dict(company_name="Initrode", title="Platform Engineer", location_raw="Somewhere Odd",
         url="https://x/5", source="ashby", distance_miles=None, remote=False,
         posted_at="2026-07-09 09:00:00"),
]
for s in seed:
    db.upsert_job(conn, s)
conn.commit()
conn.close()

c = TestClient(app)
fails = 0
def check(cond, label):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")

r = c.get("/api/jobs?filter=new").json()
# Default radius is 30mi, so the 290mi Boise job is filtered out at view time.
check(len(r["jobs"]) == 4, f"4 of 5 seeded jobs shown at 30mi (got {len(r['jobs'])})")
check(r["counts"]["new"] == 4, f"New count = 4 (got {r['counts']['new']})")

# Widening the radius in settings reveals the far job with NO refetch.
c.put("/api/settings", json={"radius_miles": 400})
r = c.get("/api/jobs?filter=new").json()
check(r["counts"]["new"] == 5, f"Radius 400 reveals Boise instantly (got {r['counts']['new']})")
c.put("/api/settings", json={"radius_miles": 30})

# Newest first
titles = [j["title"] for j in r["jobs"]]
check(titles[0] == "Software Engineer", f"Sorted newest first (got {titles[0]})")

# Distance filter keeps remote + unknown, drops the 290mi one
r = c.get("/api/jobs?filter=new").json()
got = {j["title"] for j in r["jobs"]}
check("QA Engineer" not in got, "290mi job excluded by 30mi radius")
check("Data Analyst" in got, "Remote job kept when include_remote on")
check("Platform Engineer" in got, "Unknown-distance job kept (not silently dropped)")
check("DevOps Engineer" in got, "28mi job kept under 30mi filter")

c.put("/api/settings", json={"include_remote": False})
r = c.get("/api/jobs?filter=new").json()
check("Data Analyst" not in {j["title"] for j in r["jobs"]}, "Remote excluded when toggled off in settings")
c.put("/api/settings", json={"include_remote": True})

# Search
r = c.get("/api/jobs?filter=all&q=Engineer").json()
check(all("Engineer" in j["title"] for j in r["jobs"]), "Search filters by title")

# --- State transitions ---
all_jobs = c.get("/api/jobs?filter=all").json()["jobs"]
jid = [j for j in all_jobs if j["title"] == "Software Engineer"][0]["id"]

c.post("/api/jobs/seen", json={"ids": [jid], "value": True})
r = c.get("/api/jobs?filter=new").json()
check(r["counts"]["new"] == 3, f"Marking seen drops new count to 3 (got {r['counts']['new']})")
check(jid not in [j["id"] for j in r["jobs"]], "Seen job leaves the New tab")

r = c.get("/api/jobs?filter=all").json()
check(jid in [j["id"] for j in r["jobs"]], "Seen job still visible in All tab")

c.post("/api/jobs/star", json={"ids": [jid], "value": True})
r = c.get("/api/jobs?filter=starred").json()
check(len(r["jobs"]) == 1, "Star filter works")

# Applied implies seen
jid2 = [j for j in all_jobs if j["title"] == "DevOps Engineer"][0]["id"]
c.post("/api/jobs/applied", json={"ids": [jid2], "value": True})
r = c.get("/api/jobs?filter=applied").json()
check(len(r["jobs"]) == 1, "Applied filter works")
check(r["jobs"][0]["seen_at"] is not None, "Applying implies seen")

c.post("/api/jobs/hide", json={"ids": [jid2], "value": True})
r = c.get("/api/jobs?filter=all").json()
check(jid2 not in [j["id"] for j in r["jobs"]], "Hidden job leaves All tab")

r = c.post("/api/jobs/mark_all_seen").json()
check(c.get("/api/jobs?filter=new").json()["counts"]["new"] == 0, "Mark all seen zeroes New")

# Detect endpoint shape (no network: returns unknown, must not 500)
r = c.post("/api/companies/detect", json={"name": "Zzz Fake Co"})
check(r.status_code == 200, f"Detect endpoint responds 200 (got {r.status_code})")
check(r.json()["supported"] is False, "Unknown company reports supported=false")

r = c.post("/api/companies", json={"name": "Zzz Fake Co"})
check(r.status_code == 422, f"Adding undetectable company returns 422 (got {r.status_code})")

r = c.get("/api/companies").json()
check(len(r["companies"]) == 1, "Company list works")

check(c.get("/api/health").json()["ok"], "Health endpoint")
check(c.get("/").status_code == 200, "Frontend serves")

print(f"\n{'ALL API TESTS PASS' if fails==0 else f'{fails} FAILURES'}")
sys.exit(1 if fails else 0)
