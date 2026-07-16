"""Verify the identity/dedupe core before wiring up the network.

SAFETY: this file writes to the DB with unqualified UPDATEs and must NEVER
touch a real database. It forces JOBHUNT_DATA_DIR to a throwaway temp dir
before importing app.config, which is what resolves DB_PATH at import time.
"""
import os
import sys
import tempfile

# Must happen BEFORE `from app import ...` -- config.DB_PATH is bound at import.
os.environ["JOBHUNT_DATA_DIR"] = tempfile.mkdtemp(prefix="jobhunt-test-")

from app import db, config

# Belt-and-braces: if the env var were ever ignored, fail loudly rather than
# silently stamping every row of someone's real job board.
assert "jobhunt-test-" in str(config.DB_PATH), (
    f"REFUSING TO RUN: tests would write to {config.DB_PATH}"
)

conn = db.connect()
db.init_db(conn)

# --- Hash stability across source variations -------------------------------
cases = [
    # (desc, a, b, should_match)
    ("Same job, state abbreviated vs spelled out",
     ("Acme Corp", "Software Engineer", "Lindon, UT"),
     ("Acme Corp", "Software Engineer", "Lindon, Utah"), True),
    ("Same job, country suffix added",
     ("Acme Corp", "Software Engineer", "Lindon, UT"),
     ("Acme Corp", "Software Engineer", "Lindon, UT, USA"), True),
    ("Same job, title carries (Remote) tag from one source",
     ("Acme Corp", "Software Engineer", "Remote"),
     ("Acme Corp", "Software Engineer (Remote)", "Remote"), True),
    ("Same job, req ID appended",
     ("Acme Corp", "Software Engineer", "Lindon, UT"),
     ("Acme Corp", "Software Engineer R12345", "Lindon, UT"), True),
    ("Same job, punctuation differs",
     ("Acme Corp", "Sr. Software Engineer", "Lindon, UT"),
     ("Acme Corp", "Sr Software Engineer", "Lindon, UT"), True),
    ("Different level = different job",
     ("Acme Corp", "Software Engineer II", "Lindon, UT"),
     ("Acme Corp", "Software Engineer III", "Lindon, UT"), False),
    ("Different city = different job",
     ("Acme Corp", "Software Engineer", "Lindon, UT"),
     ("Acme Corp", "Software Engineer", "Provo, UT"), False),
    ("Different company = different job",
     ("Acme Corp", "Software Engineer", "Lindon, UT"),
     ("Globex", "Software Engineer", "Lindon, UT"), False),
]

print("=== Hash stability ===")
fails = 0
for desc, a, b, should_match in cases:
    ha, hb = db.job_hash(*a), db.job_hash(*b)
    matched = ha == hb
    ok = matched == should_match
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}")
    if not ok:
        print(f"        {a} -> {ha}")
        print(f"        {b} -> {hb}")

# --- Dedupe + user state preservation --------------------------------------
print("\n=== Upsert / dedupe / state preservation ===")

job_gh = {
    "company_name": "Acme Corp", "title": "Software Engineer",
    "location_raw": "Lindon, UT", "url": "https://boards.greenhouse.io/acme/jobs/1",
    "description": "Build things.", "posted_at": "2026-07-10 00:00:00",
    "source": "greenhouse", "distance_miles": 2.1, "remote": False,
}
is_new = db.upsert_job(conn, job_gh)
conn.commit()
print(f"[{'PASS' if is_new else 'FAIL'}] First insert reports new=True")

# Same job arriving from Adzuna with a different location spelling + URL.
job_adz = {
    "company_name": "Acme Corp", "title": "Software Engineer (Remote)",
    "location_raw": "Lindon, Utah, USA", "url": "https://adzuna.com/land/ad/999",
    "description": None, "posted_at": "2026-07-11 00:00:00",
    "source": "adzuna", "distance_miles": 2.1, "remote": False,
}
is_new2 = db.upsert_job(conn, job_adz)
conn.commit()
print(f"[{'PASS' if not is_new2 else 'FAIL'}] Cross-source duplicate reports new=False")

count = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
print(f"[{'PASS' if count == 1 else 'FAIL'}] Only one row exists (got {count})")

row = conn.execute("SELECT posted_at, url FROM jobs").fetchone()
print(f"[{'PASS' if row['posted_at'] == '2026-07-10 00:00:00' else 'FAIL'}] "
      f"Original posted_at preserved, not overwritten by later source")

# Mark it seen and applied, then re-fetch: user state must survive.
_h = db.job_hash(job_gh["company_name"], job_gh["title"], job_gh.get("location_raw"))
conn.execute("UPDATE jobs SET seen_at = ?, applied_at = ?, starred = 1 WHERE job_hash = ?",
             (db.now_iso(), db.now_iso(), _h))
conn.commit()
db.upsert_job(conn, job_gh)
conn.commit()
row = conn.execute("SELECT seen_at, applied_at, starred FROM jobs").fetchone()
ok = row["seen_at"] is not None and row["applied_at"] is not None and row["starred"] == 1
print(f"[{'PASS' if ok else 'FAIL'}] Re-fetch preserves seen/applied/starred")
fails += not ok

# --- "Gone" detection ------------------------------------------------------
print("\n=== Gone detection ===")
import time
time.sleep(1.1)
future = db.now_iso()
n = db.mark_gone(conn, "greenhouse", future)
row = conn.execute("SELECT gone_at FROM jobs").fetchone()
print(f"[{'PASS' if row['gone_at'] is not None else 'FAIL'}] Job absent from new run flagged gone")

# Reappearing job clears gone_at.
db.upsert_job(conn, job_gh)
conn.commit()
row = conn.execute("SELECT gone_at FROM jobs").fetchone()
print(f"[{'PASS' if row['gone_at'] is None else 'FAIL'}] Reappearing job clears gone_at")

print(f"\n{'ALL GOOD' if fails == 0 else f'{fails} FAILURES'}")
conn.close()
