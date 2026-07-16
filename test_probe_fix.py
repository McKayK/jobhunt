"""Reproduce the false-positive bug and prove the fix.

The real failure: SmartRecruiters answers HTTP 200 with an empty result set for
a company slug that does not exist. The old probe returned True on status alone,
so ~50 invented slugs like 'klugonyx' were recorded as high-confidence.
"""
import sys
from unittest import mock
sys.path.insert(0, "/home/claude/jobhunt")

from app import detect


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
    def json(self): return self._p
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


fails = 0
def check(cond, label):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


print("=== SmartRecruiters ===")
# This is what a bogus slug actually returns: 200 + empty.
EMPTY = {"offset": 0, "limit": 1, "totalFound": 0, "content": []}
with mock.patch("app.detect._get", return_value=FakeResp(EMPTY)):
    check(detect.probe_smartrecruiters("klugonyx") is False,
          "Empty 200 response is rejected (this was the bug)")

REAL = {"offset": 0, "limit": 1, "totalFound": 42,
        "content": [{"id": "x", "name": "Software Engineer"}]}
with mock.patch("app.detect._get", return_value=FakeResp(REAL)):
    check(detect.probe_smartrecruiters("adobe") is True,
          "Real board with postings is accepted")

# A real board that happens to have 0 openings today still reports totalFound.
ZERO_BUT_REAL = {"offset": 0, "limit": 1, "totalFound": 0, "content": []}
with mock.patch("app.detect._get", return_value=FakeResp(ZERO_BUT_REAL)):
    check(detect.probe_smartrecruiters("quiet-co") is False,
          "Genuinely empty board also rejected (verify falls back to fetcher)")

with mock.patch("app.detect._get", return_value=FakeResp({}, status=404)):
    check(detect.probe_smartrecruiters("nope") is False, "404 rejected")

print("\n=== Lever ===")
with mock.patch("app.detect._get", return_value=FakeResp([])):
    check(detect.probe_lever("fake") is False, "Empty list rejected")

with mock.patch("app.detect._get", return_value=FakeResp([{"text": "Engineer"}])):
    check(detect.probe_lever("entrata") is True, "Board with postings accepted")

print("\n=== Greenhouse (was already correct) ===")
with mock.patch("app.detect._get", return_value=FakeResp({"jobs": [{"id": 1}]})):
    check(detect.probe_greenhouse("qualtrics") is True, "Real board accepted")

with mock.patch("app.detect._get", return_value=FakeResp({}, status=404)):
    check(detect.probe_greenhouse("nope") is False, "404 rejected")

print("\n=== Regression: guessing must not invent a slug ===")
# Simulate every probe seeing an empty-but-200 world.
with mock.patch("app.detect._get", return_value=FakeResp(EMPTY)), \
     mock.patch("app.detect.requests.post", return_value=FakeResp({"data": {"jobBoard": None}})), \
     mock.patch("app.detect.requests.get", return_value=FakeResp(EMPTY)):
    d = detect.detect("Klugonyx", None, probe=True)
    check(d.ats == "unknown" and d.slug is None,
          f"Nonexistent company resolves to unknown (got {d.ats}/{d.slug})")

print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURES'}")
sys.exit(1 if fails else 0)