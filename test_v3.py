"""Tests for v3: new sources, apply-link discovery, remote nationwide, scam
filter, matching, application tracking, resumes and tailoring.
Run: python test_v3.py   (no network needed — everything external is mocked)
"""
import base64
import io
import json
import os
import sys
import tempfile
import zipfile

os.environ["JOBHUNT_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import patch

import pandas as pd

from app import autodiscover, boardfinder, db, detect, geo, match, quality, refresh, settings
from app.fetchers import aggregate, ats, remote_boards

assert str(db.config.DB_PATH).startswith(tempfile.gettempdir()), "REFUSING: not a temp DB"

PASS = 0


def ok(name, cond, extra=""):
    global PASS
    assert cond, f"FAIL: {name} {extra}"
    PASS += 1
    print(f"[PASS] {name}")


class FakeResp:
    def __init__(self, payload, status=200, text=None, headers=None, url=""):
        self._p, self.status_code = payload, status
        self.text = text if text is not None else json.dumps(payload)
        self.headers = headers or {"content-type": "application/json"}
        self.ok = status < 400
        self.url = url

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# ---------------------------------------------------------------------------
# New ATS fetchers (fixtures shaped like the live APIs, Oct 2026)
# ---------------------------------------------------------------------------
WORKABLE = {"name": "Hugging Face", "jobs": [
    {"title": "ML Engineer - US Remote", "shortcode": "19A1", "url": "https://apply.workable.com/j/19A1",
     "application_url": "https://apply.workable.com/j/19A1/apply", "published_on": "2026-05-29",
     "telecommuting": True, "employment_type": "Full-time", "description": "",
     "locations": [{"city": "New York", "country": "United States", "countryCode": "US",
                    "hidden": False, "region": "New York"}]},
    {"title": "ML Engineer - EMEA", "shortcode": "81B4", "url": "https://apply.workable.com/j/81B4",
     "published_on": "2026-05-29", "telecommuting": True,
     "locations": [{"city": "Paris", "country": "France", "countryCode": "FR", "hidden": False,
                    "region": "Île-de-France"}]},
]}
with patch("app.fetchers.ats.requests.get", return_value=FakeResp(WORKABLE)):
    wk = ats.fetch_workable("Hugging Face", "huggingface")
ok("Workable parses", len(wk) == 2 and wk[0]["location_raw"].startswith("Remote - New York"))
ok("Workable remote EMEA is foreign",
   geo.remote_eligibility(wk[1]["location_raw"], "UT") == "foreign")
ok("Workable US remote is eligible",
   geo.remote_eligibility(wk[0]["location_raw"], "UT") == "us")

BAMBOO = {"meta": {"totalCount": 1}, "result": [
    {"id": "160", "jobOpeningName": "Web Developer", "departmentLabel": "IT",
     "employmentStatusLabel": "Full-Time", "isRemote": None, "locationType": "0",
     "location": {"city": "Orem", "state": "Utah"},
     "atsLocation": {"city": None, "country": None, "province": None, "state": None}}]}
with patch("app.fetchers.ats.requests.get", return_value=FakeResp(BAMBOO)):
    bb = ats.fetch_bamboohr("Local Co", "localco")
ok("BambooHR parses", bb[0]["url"] == "https://localco.bamboohr.com/careers/160"
   and bb[0]["location_raw"] == "Orem, Utah")
with patch("app.fetchers.ats.requests.get", return_value=FakeResp({}, status=302)):
    try:
        ats.fetch_bamboohr("Nope", "nope")
        raised = False
    except ValueError:
        raised = True
ok("BambooHR redirect = board doesn't exist", raised)

BREEZY = [{"id": "x1", "friendly_id": "web-dev", "name": "Web Developer",
           "url": "https://acme.breezy.hr/p/web-dev", "published_date": "2026-10-01T12:00:00.000Z",
           "type": {"id": "fullTime", "name": "Full-Time"},
           "location": {"city": "Provo", "state": {"id": "UT", "name": "Utah"},
                        "country": {"id": "US", "name": "United States"}, "is_remote": False,
                        "name": "Provo, UT"}}]
with patch("app.fetchers.ats.requests.get", return_value=FakeResp(BREEZY)):
    bz = ats.fetch_breezy("Acme", "acme")
ok("Breezy parses", bz[0]["location_raw"] == "Provo, UT" and bz[0]["job_type"] == "Full-Time")

RECRUITEE = {"offers": [{"title": "QA Engineer", "careers_url": "https://acme.recruitee.com/o/qa",
                         "location": "Lehi, Utah, United States", "remote": False, "status": "published",
                         "published_at": "2026-10-02 10:00:00 UTC", "description": "<p>Test stuff</p>",
                         "requirements": "<p>Cypress</p>"}]}
with patch("app.fetchers.ats.requests.get", return_value=FakeResp(RECRUITEE)):
    rc = ats.fetch_recruitee("Acme", "acme")
ok("Recruitee parses", rc[0]["title"] == "QA Engineer" and "Cypress" in rc[0]["description"])

# Workday: link now includes the site segment; myworkdaysite.com style supported.
ok("Workday ids (classic)", ats.workday_ids("adobe.wd5.myworkdayjobs.com", "/en-US/external_experienced")
   == ("adobe", "external_experienced"))
ok("Workday ids (myworkdaysite)", ats.workday_ids("wd1.myworkdaysite.com", "/recruiting/acme/External")
   == ("acme", "External"))
d = detect.detect_from_url("https://domo.wd12.myworkdayjobs.com/en-US/DomoCareers/job/Utah/Engineer_R123")
ok("Workday apply link -> board", d.ats == "workday" and d.workday_host == "domo.wd12.myworkdayjobs.com"
   and d.workday_path == "/en-US/DomoCareers")
d = detect.detect_from_url("https://wd1.myworkdaysite.com/en-US/recruiting/acme/External/job/X_R1")
ok("myworkdaysite apply link -> board", d and d.ats == "workday" and d.slug == "acme")
ok("iCIMS recognized as link-only", detect.detect_from_url("https://careers-acme.icims.com/jobs/123").ats == "link")
ok("Greenhouse EU board recognized", detect.detect_from_url("https://boards.eu.greenhouse.io/nice/jobs/4837").slug == "nice")
ok("BambooHR URL recognized", detect.detect_from_url("https://localco.bamboohr.com/careers/160").ats == "bamboohr")

# ---------------------------------------------------------------------------
# Remote boards
# ---------------------------------------------------------------------------
HIMALAYAS = {"jobs": [
    {"title": "Full Stack Developer", "companyName": "Lemon", "applicationLink": "https://himalayas.app/j/1",
     "locationRestrictions": ["Canada", "United States", "Mexico"], "pubDate": 1790075996,
     "description": "<p>React and Node</p>", "minSalary": None, "maxSalary": None, "salaryPeriod": "annual"},
    {"title": "Backend Developer", "companyName": "EuroCo", "applicationLink": "https://himalayas.app/j/2",
     "locationRestrictions": ["Germany", "France"], "pubDate": 1790075996, "description": "x"},
    {"title": "Frontend Developer", "companyName": "Anywhere Inc", "applicationLink": "https://himalayas.app/j/3",
     "locationRestrictions": [], "pubDate": 1790075996, "description": "x",
     "minSalary": 90000, "maxSalary": 120000, "salaryPeriod": "annual"},
]}
with patch("app.fetchers.remote_boards.requests.get", return_value=FakeResp(HIMALAYAS)):
    hm = remote_boards.fetch_himalayas(["developer"])
ok("Himalayas keeps US-eligible, drops EU-only", sorted(j["company_name"] for j in hm) == ["Anywhere Inc", "Lemon"])
ok("Himalayas pubDate in seconds parsed", hm[0]["posted_at"].startswith("2026-"))
ok("Himalayas salary", any(j["salary_text"] == "$90k–$120k/yr" for j in hm))

REMOTIVE = {"jobs": [
    {"title": "Dev", "company_name": "A", "url": "https://remotive.com/1", "publication_date": "2026-10-17T16:04:29",
     "candidate_required_location": "USA, Canada", "description": "<p>x</p>", "salary": "$100k"},
    {"title": "Dev", "company_name": "B", "url": "https://remotive.com/2", "publication_date": "2026-10-17T16:04:29",
     "candidate_required_location": "Europe", "description": "<p>x</p>"},
]}
with patch("app.fetchers.remote_boards.requests.get", return_value=FakeResp(REMOTIVE)):
    rm = remote_boards.fetch_remotive(["developer"])
ok("Remotive drops Europe-only", [j["company_name"] for j in rm] == ["A"])

REMOTEOK = [{"legal": "link back"},
            {"id": "1", "position": "Senior React Developer", "company": "X", "tags": ["react"],
             "location": "", "url": "https://remoteOK.com/1", "epoch": 1792350002, "salary_min": 0, "salary_max": 0,
             "description": "x"},
            {"id": "2", "position": "Sales Manager", "company": "Y", "tags": ["sales"], "location": "",
             "url": "https://remoteOK.com/2", "epoch": 1792350002}]
with patch("app.fetchers.remote_boards.requests.get", return_value=FakeResp(REMOTEOK)):
    ro = remote_boards.fetch_remoteok(["developer", "react"])
ok("RemoteOK keyword filter", [j["company_name"] for j in ro] == ["X"])

# ---------------------------------------------------------------------------
# Job sites row parsing (JobSpy 1.4 columns)
# ---------------------------------------------------------------------------
row = {"title": "Web Developer", "company": "Domo", "job_url": "https://www.indeed.com/viewjob?jk=abc",
       "job_url_direct": "https://domo.wd12.myworkdayjobs.com/DomoCareers/job/Utah/Web-Dev_R1",
       "location": "American Fork, UT, US", "is_remote": False, "date_posted": "2026-10-15",
       "description": "React, TypeScript, Node.js, AWS. 3+ years of experience.",
       "min_amount": 85000.0, "max_amount": 110000.0, "interval": "yearly", "company_url": "https://www.indeed.com/cmp/Domo",
       "company_url_direct": "https://www.domo.com", "emails": None, "company_rating": 3.9, "company_reviews_count": 412.0}
j = aggregate._row_to_job(row, "indeed")
ok("Indeed direct apply link kept", j["apply_url"].startswith("https://domo.wd12"))
ok("Indeed salary formatted", j["salary_text"] == "$85k–$110k/yr")
ok("Plan size counts remote searches", aggregate.plan_size(["indeed", "google"], ["developer"], True) == 3)

# ---------------------------------------------------------------------------
# DB: keyword matching, upgrade path, expiry
# ---------------------------------------------------------------------------
conn = db.connect()
db.init_db(conn)
ok("intern doesn't match Internal", db.kw_match("Internal Tools Engineer", "intern") == 0)
ok("intern matches Software Intern", db.kw_match("Software Intern", "intern") == 1)
ok("'front end' matches Front-End", db.kw_match("Front-End Developer", "front end") == 1)
ok("c++ keyword works", db.kw_match("C++ Developer", "c++") == 1)

agg = {"company_name": "Podium", "title": "Software Engineer", "location_raw": "Lehi, UT",
       "url": "https://www.indeed.com/viewjob?jk=1", "apply_url": "https://job-boards.greenhouse.io/podium81/jobs/9",
       "source": "indeed", "description": "short"}
db.upsert_job(conn, agg)
cid = db.add_company(conn, "Podium", None, "greenhouse", "podium81")
board = dict(agg, url="https://job-boards.greenhouse.io/podium81/jobs/9", source="greenhouse", company_id=cid,
             apply_url=None, description="Full description from the board")
db.upsert_job(conn, board)
r = conn.execute("SELECT source, url, company_id, description FROM jobs").fetchone()
ok("Board sighting upgrades a job-site row", r["source"] == "greenhouse" and "greenhouse" in r["url"]
   and r["company_id"] == cid)
conn.execute("UPDATE jobs SET gone_at = '2026-01-01 00:00:00'")
db.upsert_job(conn, agg)   # Indeed still lists it after the board closed it
r = conn.execute("SELECT gone_at, url FROM jobs").fetchone()
ok("Job site can't resurrect a board-closed job", r["gone_at"] is not None and "greenhouse" in r["url"])

old = {"company_name": "Old Co", "title": "Dev", "location_raw": "Provo, UT",
       "url": "https://www.indeed.com/viewjob?jk=2", "source": "indeed"}
db.upsert_job(conn, old)
conn.execute("UPDATE jobs SET last_seen_at = '2020-01-01 00:00:00' WHERE company_name='Old Co'")
n = db.expire_aggregate(conn, 10)
ok("Job-site postings expire by age", n == 1)
conn.execute("DELETE FROM jobs"); conn.execute("DELETE FROM companies"); conn.commit()

# re-adding a company fixes its Workday host (the Domo bug)
db.add_company(conn, "Domo", None, "workday", "domo", "domo.wd5.myworkdayjobs.com", "/en-US/Domo")
db.add_company(conn, "Domo", None, "workday", "domo", "domo.wd12.myworkdayjobs.com", "/DomoCareers")
r = conn.execute("SELECT workday_host, workday_path FROM companies WHERE slug='domo'").fetchone()
ok("Re-adding fixes Workday host/path", r["workday_host"] == "domo.wd12.myworkdayjobs.com" and r["workday_path"] == "/DomoCareers")
conn.execute("DELETE FROM companies"); conn.commit()

# ---------------------------------------------------------------------------
# Scam filter
# ---------------------------------------------------------------------------
scam = {"title": "Remote Data Entry Clerk", "company_name": "Global Solutions", "source": "indeed",
        "description": ("Work from home! Earn $1,500 per week. No interview required. Interviews are held on "
                        "Telegram. We will send you a check to purchase your equipment. Contact hr.globalsol@gmail.com"),
        "salary_text": "$45/hr", "location_raw": "Remote"}
s, flags, _ = quality.assess(scam)
ok("Obvious scam scores >= 60", s >= 60, f"score={s} {flags}")
legit = {"title": "Software Engineer", "company_name": "Podium", "source": "greenhouse", "company_id": 1,
         "description": "Podium will never ask for your SSN or bank account details. Build React apps." * 5}
s2, flags2, _ = quality.assess(legit)
ok("Board job with anti-scam disclaimer scores 0", s2 == 0, f"{flags2}")
_, _, staff = quality.assess({"title": "Developer", "company_name": "Insight Global", "source": "indeed",
                              "description": "x" * 400})
ok("Staffing agency detected", staff)
_, _, staff2 = quality.assess({"title": "Nurse", "company_name": "Hays Medical Center", "source": "indeed",
                               "description": "x" * 400})
ok("'Hays Medical Center' isn't a staffing agency", not staff2)

# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
RESUME = ("McKay Kleinman\nFull Stack Developer\nOrem, UT\nSkills: JavaScript, TypeScript, Python, SQL, React, "
          "Next.js, React Native, Node.js, Express, FastAPI, PostgreSQL, SQLite, Firebase, Docker, Git, Playwright.\n"
          "Experience: built e-commerce system, troubleshot printers and POS, installed VoIP phone system.") * 2
rs = match.skills_in(RESUME)
sc, det = match.score(rs, "Full Stack Developer", "We use React, Node.js and PostgreSQL. Docker a plus. AWS. 2+ years experience.")
ok("Good match scores high", sc >= 70, f"{sc} {det}")
sc2, _ = match.score(rs, "Senior Java Engineer", "Java, Spring Boot, Kafka, Kubernetes, Scala required.")
ok("Poor match scores low", sc2 < 45, f"{sc2}")
ok("Years required extracted", det["years"] == 2)

# ---------------------------------------------------------------------------
# Auto-discovery from apply links
# ---------------------------------------------------------------------------
site_jobs = [
    {"company_name": "Domo", "geo_status": "in_range",
     "apply_url": "https://domo.wd12.myworkdayjobs.com/DomoCareers/job/Utah/Dev_R1"},
    {"company_name": "Far Away Inc", "geo_status": "too_far",
     "apply_url": "https://job-boards.greenhouse.io/faraway/jobs/1"},
    {"company_name": "Lucid", "geo_status": "in_range",
     "apply_url": "https://job-boards.greenhouse.io/lucidsoftware/jobs/7"},
]
probed = []


def fake_probe(d):
    probed.append((d.ats, d.slug))
    return d.slug != "lucidsoftware"


with patch("app.autodiscover.detect.probe_detection", side_effect=fake_probe), \
     patch("app.autodiscover.ats_fetchers.fetch_company", return_value=[]):
    found = autodiscover.promote_apply_links(conn, site_jobs)
ok("Workday company discovered from apply link", [r["name"] for r, _ in found] == ["Domo"])
ok("Out-of-range companies ignored", ("greenhouse", "faraway") not in probed)
dom = conn.execute("SELECT * FROM companies WHERE name='Domo'").fetchone()
ok("Discovered Workday row has host + path", dom["workday_host"] == "domo.wd12.myworkdayjobs.com"
   and dom["workday_path"] == "/DomoCareers" and dom["added_by"] == "auto")
probed.clear()
with patch("app.autodiscover.detect.probe_detection", side_effect=fake_probe):
    autodiscover.promote_apply_links(conn, site_jobs)
ok("Failed boards aren't re-probed next refresh", probed == [])

# ---------------------------------------------------------------------------
# Board finder
# ---------------------------------------------------------------------------
conn.execute("""INSERT INTO jobs (job_hash, company_name, title, url, apply_url, source)
                VALUES ('h1', 'Acme Widgets', 'Dev', 'https://indeed.com/x',
                        'https://acme.wd1.myworkdayjobs.com/en-US/External/job/Provo/Dev_R9', 'indeed')""")
conn.commit()
with patch("app.boardfinder.detect.probe_detection", return_value=True):
    res = boardfinder.find_board(conn, "Acme Widgets", use_search=False, use_ai=False)
ok("Finder uses stored apply links first", res["supported"] and res["ats"] == "workday"
   and res["method"] == "apply link on a posting", f"{res}")

no_board = detect.Detection("link", None, confidence="medium", note="iCIMS",
                            url="https://careers-zeta.icims.com/jobs")
with patch("app.boardfinder.detect.detect", return_value=no_board):
    res = boardfinder.find_board(conn, "Zeta Corp", use_search=False, use_ai=False)
ok("Finder returns the careers link for unreadable systems",
   res["found"] and not res["supported"] and "icims" in res["careers_url"], f"{res}")

DDG_HTML = ('<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fjobs.lever.co%2Fzeta&rut=1">Zeta</a>'
            '<a class="result__a" href="https://www.zeta.com/careers">Careers</a>')
with patch("app.boardfinder.requests.get", return_value=FakeResp({}, text=DDG_HTML)):
    urls = boardfinder._ddg_search("zeta careers")
ok("DuckDuckGo result links decoded", urls[:2] == ["https://jobs.lever.co/zeta", "https://www.zeta.com/careers"])

# ---------------------------------------------------------------------------
# Full refresh with every network call mocked
# ---------------------------------------------------------------------------
conn.execute("DELETE FROM jobs"); conn.execute("DELETE FROM companies")
conn.execute("DELETE FROM meta WHERE key LIKE 'discover:%'"); conn.commit()
settings.set_many(conn, {"home_zip": "84042", "radius_miles": 30, "include_keywords": ["developer"],
                         "exclude_keywords": ["senior"], "use_google": False, "use_linkedin": False,
                         "remote_nationwide": True, "use_remote_boards": True, "include_remote": True,
                         "auto_discover": True, "hide_risky": True})
db.add_company(conn, "Weave", None, "ashby", "weave")

BOARD_JOBS = [{"company_name": "Weave", "title": "Frontend Developer", "location_raw": "Lehi, Utah",
               "url": "https://jobs.ashbyhq.com/weave/1", "description": "React TypeScript", "source": "ashby"}]


def fake_scrape(site, **kw):
    if kw.get("is_remote"):
        rows = [{"title": "Remote Web Developer", "company": "Nation Co", "job_url": "https://indeed.com/r1",
                 "location": "Denver, CO, US", "is_remote": True, "description": "React Node " * 40},
                {"title": "Remote Developer (California only)", "company": "Cal Co", "job_url": "https://indeed.com/r2",
                 "location": "Remote - California", "is_remote": True, "description": "x" * 400}]
    else:
        rows = [{"title": "Web Developer", "company": "Domo", "job_url": "https://indeed.com/1",
                 "job_url_direct": "https://domo.wd12.myworkdayjobs.com/DomoCareers/job/Utah/Web-Dev_R1",
                 "location": "American Fork, UT, US", "is_remote": False, "description": "React " * 80},
                {"title": "Data Entry Developer", "company": "Unknown company", "job_url": "https://indeed.com/2",
                 "location": "Provo, UT, US", "is_remote": False,
                 "description": "Earn $2,000 per week! Interview on Telegram. We send a check to purchase equipment."},
                {"title": "Senior Developer", "company": "Big Co", "job_url": "https://indeed.com/3",
                 "location": "Provo, UT, US", "is_remote": False, "description": "x" * 400}]
    return pd.DataFrame(rows)


DOMO_JOBS = [{"company_name": "Domo", "title": "Web Developer", "location_raw": "American Fork, UT",
              "url": "https://domo.wd12.myworkdayjobs.com/DomoCareers/job/Utah/Web-Dev_R1",
              "source": "workday", "description": None}]


def fake_fetch_company(name, ats_, slug, row=None):
    return {"weave": BOARD_JOBS, "domo": DOMO_JOBS}.get(slug, [])


with patch("app.fetchers.aggregate._scrape", side_effect=fake_scrape), \
     patch("app.refresh.ats_fetchers.fetch_company", side_effect=fake_fetch_company), \
     patch("app.autodiscover.ats_fetchers.fetch_company", side_effect=fake_fetch_company), \
     patch("app.autodiscover.detect.probe_detection", return_value=True), \
     patch("app.autodiscover.detect.detect", return_value=detect.Detection("unknown")), \
     patch.dict(remote_boards.BOARDS, {"himalayas": lambda t: remote_boards._job and [
         remote_boards._job("Remote Corp", "React Developer", "https://himalayas.app/j/9", "Remote - US",
                            "React Node " * 40, "2026-10-18 00:00:00", "himalayas")],
         "jobicy": lambda t: [], "remotive": lambda t: [], "remoteok": lambda t: []}), \
     patch("app.geo._http_geocode", return_value=None):
    totals = refresh.refresh_all(conn, verbose=False)

ok("Refresh completes", refresh.PROGRESS.get("phase") == "done", str(refresh.PROGRESS))
ok("Domo auto-tracked from Indeed apply link", totals["discovered"] >= 1 and
   conn.execute("SELECT 1 FROM companies WHERE name='Domo' AND ats='workday'").fetchone() is not None)
r = conn.execute("SELECT source, url, apply_url FROM jobs WHERE company_name='Domo'").fetchone()
ok("Domo job now sourced from its own board", r["source"] == "workday", dict(r))
r = conn.execute("SELECT geo_status FROM jobs WHERE company_name='Cal Co'").fetchone()
ok("State-restricted remote job tagged", r["geo_status"] == "remote_other_state")
r = conn.execute("SELECT risk_score FROM jobs WHERE title='Data Entry Developer'").fetchone()
ok("Scam posting scored risky", r["risk_score"] >= 60, r["risk_score"])
ok("Remote-board job stored", conn.execute("SELECT 1 FROM jobs WHERE source='himalayas'").fetchone() is not None)
ok("Remote board interval recorded", conn.execute("SELECT 1 FROM meta WHERE key='board_last:himalayas'").fetchone() is not None)

# ---------------------------------------------------------------------------
# API: views, tracking, resumes, tailoring
# ---------------------------------------------------------------------------
from fastapi.testclient import TestClient
from app import server

with TestClient(server.app) as c:
    new = c.get("/api/jobs?filter=new").json()
    titles = {j["title"] for j in new["jobs"]}
    ok("New tab: local + nationwide remote", {"Web Developer", "Remote Web Developer", "React Developer"} <= titles, titles)
    ok("Scam hidden from New", "Data Entry Developer" not in titles)
    ok("Excluded keyword (senior) hidden", "Senior Developer" not in titles)
    ok("Other-state remote hidden", "Remote Developer (California only)" not in titles)
    flagged = c.get("/api/jobs?filter=flagged").json()
    ok("Scam visible under Flagged", [j["title"] for j in flagged["jobs"]] == ["Data Entry Developer"])

    jid = next(j["id"] for j in new["jobs"] if j["title"] == "Web Developer")
    c.post("/api/jobs/seen", json={"ids": [jid], "value": True})
    recent = c.get("/api/jobs?filter=recent").json()
    ok("Opened job findable under Recently viewed", [j["id"] for j in recent["jobs"]] == [jid])
    c.post(f"/api/jobs/{jid}/status", json={"status": "interviewing"})
    apps = c.get("/api/jobs?filter=applications").json()["jobs"]
    ok("Status moves job into Applications", apps[0]["id"] == jid and apps[0]["app_status"] == "interviewing")
    c.put(f"/api/jobs/{jid}/notes", json={"text": "Talked to Sam"})
    det = c.get(f"/api/jobs/{jid}?fetch=false").json()
    ok("Job detail with notes", det["notes"] == "Talked to Sam" and det["applied_at"])

    c.post("/api/block", json={"company": "Nation Co"})
    titles = {j["title"] for j in c.get("/api/jobs?filter=all").json()["jobs"]}
    ok("Blocked company hidden", "Remote Web Developer" not in titles)

    # Resume upload (as a generated .docx) and match scores.
    import docx
    d = docx.Document()
    for line in RESUME.split("\n"):
        d.add_paragraph(line)
    buf = io.BytesIO(); d.save(buf)
    r = c.post("/api/resumes", json={"name": "Developer", "filename": "resume.docx",
                                     "data_base64": base64.b64encode(buf.getvalue()).decode()}).json()
    ok("Resume upload (docx) extracts skills", "React" in r["skills"] and r["rescored"] > 0, r)
    j = c.get("/api/jobs?filter=all&sort=match").json()["jobs"][0]
    ok("Jobs get match scores; sort by match works", j["match_score"] is not None)

    # AI keys are write-only.
    st = c.put("/api/ai/keys", json={"anthropic_api_key": "sk-ant-test-123"}).json()
    ok("AI key saved, not echoed", st["provider"] == "anthropic" and "sk-ant" not in json.dumps(st))

    TAILORED = {"fit_summary": "Strong frontend fit.", "match_score": 82, "strengths": ["React"],
                "gaps": ["AWS"], "red_flags": [], "changes": ["Moved React projects up"], "keywords_used": ["React"],
                "resume": {"name": "McKay Kleinman", "headline": "Full Stack Developer",
                           "contact": ["Orem, UT"], "summary": "Developer.",
                           "skills": [{"category": "Front End", "items": ["React", "Next.js"]}],
                           "experience": [{"company": "Kleinman Development", "location": "Orem, UT",
                                           "dates": "2022 – Present", "title": "Developer", "bullets": ["Built apps"],
                                           "projects": [{"name": "Group Recipe", "tech": "Next.js", "bullets": ["Shared recipes"]}]}],
                           "projects": [{"name": "jobhunt", "tech": "FastAPI", "bullets": ["Job board"]}],
                           "education": ["Devmountain, 2022"]},
                "cover_letter": "Dear team,\n\nI'd love to help.\n\nMcKay"}
    conn.execute("UPDATE jobs SET description = ? WHERE id = ?", ("React developer role. " * 30, jid)); conn.commit()
    with patch("app.ai.complete", return_value=("```json\n" + json.dumps(TAILORED) + "\n```", "claude-sonnet-5-5")):
        t = c.post(f"/api/jobs/{jid}/tailor", json={"cover_letter": True}).json()
    ok("Tailor endpoint returns content", t["content"]["match_score"] == 82)
    r = c.get(f"/api/tailored/{t['id']}/resume.docx")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml = zf.read("word/document.xml").decode()
    ok("Tailored resume downloads as a valid .docx", "McKay Kleinman" in xml and "Group Recipe" in xml)
    ok("Cover letter downloads", c.get(f"/api/tailored/{t['id']}/cover.docx").status_code == 200)
    ok("Plain-text version", "TECHNICAL SKILLS" in c.get(f"/api/tailored/{t['id']}/resume.txt").text)
    det = c.get(f"/api/jobs/{jid}?fetch=false").json()
    ok("Tailored version listed on the job", len(det["tailored"]) == 1)

    # Bookmark-only company + starter import
    r = c.post("/api/companies", json={"name": "Zeta", "ats": "link",
                                       "careers_url": "https://careers-zeta.icims.com/jobs"}).json()
    ok("Link-only company saved", r["ats"] == "link")
    ok("Link-only companies are never fetched", all(x["ats"] != "link" for x in db.active_companies(conn)))
    n = c.post("/api/companies/import-starter").json()["imported"]
    ok("Starter list imports", n >= 30)

print(f"\nALL {PASS} PASSED")
