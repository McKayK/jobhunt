"""Test the URL scanner that discovery uses to read ATS identifiers from traffic."""
import sys
sys.path.insert(0, "/home/claude/jobhunt")

from app.discover import _scan

fails = 0
def check(cond, label):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")

print("=== Real network URLs a careers page would fire ===")

cases = [
    # (url, expected_ats, expected_slug)
    ("https://api.smartrecruiters.com/v1/companies/NuSkinEnterprises/postings?limit=100",
     "smartrecruiters", "NuSkinEnterprises"),
    ("https://jobs.smartrecruiters.com/TheNielsenCompany",
     "smartrecruiters", "TheNielsenCompany"),
    ("https://boards-api.greenhouse.io/v1/boards/lucidsoftware/jobs?content=true",
     "greenhouse", "lucidsoftware"),
    ("https://boards.greenhouse.io/embed/job_board?for=podium81",
     "greenhouse", "podium81"),
    ("https://api.lever.co/v0/postings/entrata?mode=json",
     "lever", "entrata"),
    ("https://jobs.lever.co/gabbwireless/abc-123",
     "lever", "gabbwireless"),
    ("https://jobs.ashbyhq.com/weave/some-job-id",
     "ashby", "weave"),
    ("https://apply.workable.com/api/v3/accounts/goreact/jobs",
     "workable", "goreact"),
]
for url, ats, slug in cases:
    f = _scan(url)
    ok = f is not None and f.ats == ats and f.slug == slug
    check(ok, f"{ats}/{slug}" + ("" if ok else f"  GOT {f.ats+'/'+f.slug if f else 'None'}"))

print("\n=== Case sensitivity (SmartRecruiters docs: identifiers are case-sensitive) ===")
f = _scan("https://api.smartrecruiters.com/v1/companies/NuSkinEnterprises/postings")
check(f.slug == "NuSkinEnterprises", f"SmartRecruiters case preserved (got {f.slug})")
f = _scan("https://boards-api.greenhouse.io/v1/boards/LucidSoftware/jobs")
check(f.slug == "lucidsoftware", f"Greenhouse lowercased (got {f.slug})")

print("\n=== Workday ===")
f = _scan("https://pluralsight.wd1.myworkdayjobs.com/wday/cxs/pluralsight/Careers/jobs")
check(f is not None and f.ats == "workday", "Workday CXS endpoint detected")
check(f.workday_host == "pluralsight.wd1.myworkdayjobs.com",
      f"Workday host captured (got {f.workday_host})")

print("\n=== Noise must not produce a slug ===")
noise = [
    "https://www.google-analytics.com/collect",
    "https://cdn.example.com/static/js/main.js",
    "https://fonts.googleapis.com/css2?family=Inter",
    "https://www.nuskin.com/careers",
    "https://boards.greenhouse.io/embed/job_board.js",
]
for url in noise:
    f = _scan(url)
    # job_board.js resolves to a blocked slug; the rest shouldn't match at all.
    ok = f is None or f.slug not in ("", "js", "static", "embed", "job_board")
    check(f is None or ok, f"ignored: {url[:52]}")

check(_scan("https://boards.greenhouse.io/embed/job_board?for=") is None,
      "Empty slug rejected")

print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURES'}")
sys.exit(1 if fails else 0)