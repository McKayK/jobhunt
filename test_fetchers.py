"""Validate fetcher parsing using fixtures shaped like the real API responses."""
import sys, json
from unittest import mock
sys.path.insert(0, "/home/claude/jobhunt")

from app.fetchers import ats

GH = {"jobs": [
    {"id": 1, "title": "Software Engineer, Backend",
     "location": {"name": "Lindon, UT"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
     "content": "&lt;p&gt;Build &lt;strong&gt;great&lt;/strong&gt; systems.&lt;/p&gt;",
     "updated_at": "2026-07-12T10:30:00-04:00"},
    {"id": 2, "title": "Data Analyst", "location": {"name": "Remote - US"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
     "content": "&lt;div&gt;Analyze   things.&lt;/div&gt;", "updated_at": "2026-07-13T08:00:00Z"},
]}

LEVER = [
    {"text": "Senior Frontend Engineer",
     "categories": {"location": "Provo, UT", "team": "Eng"},
     "hostedUrl": "https://jobs.lever.co/globex/xyz",
     "descriptionPlain": "React work.", "createdAt": 1752000000000},
]

ASHBY = {"data": {"jobBoard": {"jobPostings": [
    {"id": "abc-123", "title": "Platform Engineer", "locationName": "Salt Lake City, UT",
     "employmentType": "FullTime", "publishedAt": "2026-07-11T12:00:00.000Z"},
]}}}

SR = {"content": [
    {"id": "p1", "name": "QA Engineer",
     "location": {"city": "Draper", "region": "UT", "country": "us", "remote": False},
     "applyUrl": "https://jobs.smartrecruiters.com/Initech/p1",
     "releasedDate": "2026-07-09T00:00:00.000Z"},
], "totalFound": 1}


class FakeResp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p
    def raise_for_status(self): pass


def test(name, fn, payload, post=False):
    target = "app.fetchers.ats.requests.post" if post else "app.fetchers.ats.requests.get"
    with mock.patch(target, return_value=FakeResp(payload)):
        jobs = fn()
    print(f"--- {name} ---")
    for j in jobs:
        assert j["title"], "missing title"
        assert j["url"], "missing url"
        assert j["source"] == name.lower().split()[0], f"bad source {j['source']}"
        print(f"  {j['title'][:38]:<40} | {str(j['location_raw'])[:22]:<24} "
              f"| {j['posted_at']}")
        if j.get("description"):
            print(f"      desc: {j['description'][:60]!r}")
    return jobs


gh = test("greenhouse", lambda: ats.fetch_greenhouse("Acme", "acme"), GH)
assert gh[0]["description"] == "Build great systems.", f"HTML strip failed: {gh[0]['description']!r}"
assert gh[0]["posted_at"] == "2026-07-12 14:30:00", f"TZ convert failed: {gh[0]['posted_at']}"

lv = test("lever", lambda: ats.fetch_lever("Globex", "globex"), LEVER)
assert lv[0]["posted_at"] is not None, "epoch millis parse failed"

ab = test("ashby", lambda: ats.fetch_ashby("Initrode", "initrode"), ASHBY, post=True)
assert ab[0]["url"].endswith("/initrode/abc-123"), f"url build failed: {ab[0]['url']}"

sr = test("smartrecruiters", lambda: ats.fetch_smartrecruiters("Initech", "Initech"), SR)
assert sr[0]["location_raw"] == "Draper, UT, us", f"loc join failed: {sr[0]['location_raw']}"

print("\nHTML stripping edge cases:")
print("  nested+entities:", repr(ats.strip_html("<p>A &amp; B<br/><b>C</b></p>")))
print("  None passthrough:", ats.strip_html(None))

print("\nALL FETCHER PARSING OK")
