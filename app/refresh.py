"""The refresh pipeline: fetch (parallel) -> geo-annotate -> dedupe -> store.

What changed vs the original:
- Company boards are fetched in PARALLEL (they're network-bound; this alone
  turns a minutes-long serial crawl into seconds).
- Aggregate sources (Indeed / ZipRecruiter / Google via JobSpy) run too, which
  finds jobs at any company near your ZIP, not just listed ones.
- Title keywords are NOT applied here anymore. Everything gets stored with a
  geo annotation, and keyword + radius filtering happens at query time — so
  changing your keywords or radius in the UI is instant, no refetch.
- Progress is published to a module-level dict the UI polls, so the Refresh
  button shows what's happening instead of freezing.

We still skip storing foreign postings (a Utah board's Dublin office isn't
coming back into range by tweaking a slider), but too-far US jobs ARE stored,
so widening your radius later reveals them immediately.
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, geo, settings
from .fetchers import ats as ats_fetchers
from .fetchers import aggregate

# --- Live progress, polled by /api/refresh/status ---------------------------

PROGRESS: dict = {"running": False}
_progress_lock = threading.Lock()


def _prog(**kw):
    with _progress_lock:
        PROGRESS.update(kw)


def _fetch_one_company(company: sqlite3.Row) -> tuple[sqlite3.Row, list[dict], str | None]:
    """Network fetch only — no DB access, safe to run in a worker thread."""
    try:
        jobs = ats_fetchers.fetch_company(
            company["name"], company["ats"], company["slug"], company)
        return company, jobs, None
    except Exception as e:
        return company, [], f"{type(e).__name__}: {str(e)[:200]}"


def _ingest(conn: sqlite3.Connection, jobs: list[dict], company_id: int | None,
            home: tuple[float, float], radius: float,
            budget: geo.GeoBudget, totals: dict):
    """Geo-annotate and upsert a batch of jobs on the main thread."""
    for job in jobs:
        r = geo.resolve_location(conn, job.get("location_raw"),
                                 home=home, radius=radius, budget=budget)
        job["geo_status"] = r["status"]
        job["distance_miles"] = r["distance_miles"]
        job["lat"], job["lon"] = r["lat"], r["lon"]
        job["country"] = r["country"]
        job["remote"] = r["status"] == "remote"

        if r["status"] == "foreign":
            totals["dropped_foreign"] += 1
            continue
        if r["status"] == "unknown":
            totals["unknown"] += 1

        job["company_id"] = company_id
        totals["stored"] += 1
        if db.upsert_job(conn, job):
            totals["new"] += 1


def refresh_all(conn: sqlite3.Connection, verbose: bool = True) -> dict:
    with _progress_lock:
        PROGRESS.clear()
        PROGRESS.update(running=True, phase="start", done=0, total=0,
                        new=0, discovered=0, detail="Starting…")
    run_started = db.now_iso()
    cur = conn.execute(
        "INSERT INTO fetch_runs (started_at, source) VALUES (?, 'all') RETURNING id",
        (run_started,),
    )
    run_id = cur.fetchone()["id"]
    conn.commit()

    cfg = settings.get_all(conn)
    home = geo.home_coords(conn)
    radius = cfg["radius_miles"]
    budget = geo.GeoBudget(40)

    totals = {"found": 0, "stored": 0, "new": 0, "companies": 0,
              "errors": [], "dropped_foreign": 0, "unknown": 0, "discovered": 0}

    # --- Phase 1: company ATS boards, fetched in parallel -------------------
    companies = db.active_companies(conn) if cfg["use_company_boards"] else []
    totals["companies"] = len(companies)
    sources_seen: set[str] = set()

    if companies:
        _prog(running=True, phase="companies", done=0, total=len(companies),
              detail="Fetching company boards…", new=0)
        results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_one_company, c): c for c in companies}
            done = 0
            for fut in as_completed(futures):
                company, jobs, err = fut.result()
                results.append((company, jobs, err))
                done += 1
                _prog(done=done, detail=f"Fetched {company['name']}"
                      + (f" — ERROR" if err else f" ({len(jobs)} jobs)"))

        # DB writes stay on this thread; SQLite likes it that way.
        for company, jobs, err in results:
            if err:
                totals["errors"].append(f"{company['name']}: {err}")
                conn.execute(
                    "UPDATE companies SET last_fetch_at = ?, last_error = ? WHERE id = ?",
                    (db.now_iso(), err[:500], company["id"]))
            else:
                totals["found"] += len(jobs)
                sources_seen.update(j["source"] for j in jobs)
                _ingest(conn, jobs, company["id"], home, radius, budget, totals)
                conn.execute(
                    "UPDATE companies SET last_fetch_at = ?, last_error = NULL WHERE id = ?",
                    (db.now_iso(), company["id"]))
            conn.commit()
            _prog(new=totals["new"])
            if verbose:
                flag = f"  ERROR {err}" if err else ""
                print(f"  {company['name']:<28} found={len(jobs)}{flag}")

    # --- Phase 2: aggregate sites (any company near you) ---------------------
    sites = [s for s, key in aggregate.SITE_SETTING_MAP.items() if cfg[key]]
    if sites:
        n_terms = max(1, len(cfg["include_keywords"]))
        _prog(running=True, phase="aggregate", done=0, total=len(sites) * n_terms,
              detail="Searching job sites…")
        agg_done = [0]

        def on_progress(msg):
            agg_done[0] += 1
            _prog(done=min(agg_done[0], len(sites) * n_terms), detail=msg)

        agg_jobs, agg_errors = aggregate.fetch_aggregate(
            sites=sites,
            home_zip=cfg["home_zip"],
            radius_miles=radius,
            search_terms=cfg["include_keywords"],
            results_wanted=cfg["results_per_site"],
            hours_old=cfg["aggregate_hours_old"],
            on_progress=on_progress,
        )
        totals["found"] += len(agg_jobs)
        totals["errors"].extend(agg_errors)
        sources_seen.update(j["source"] for j in agg_jobs)
        _prog(detail=f"Processing {len(agg_jobs)} postings…")
        _ingest(conn, agg_jobs, None, home, radius, budget, totals)
        conn.commit()
        _prog(new=totals["new"])

        # --- Phase 3: auto-discover ATS boards for nearby companies ----------
        # Aggregators are the wide net; this promotes their in-range finds to
        # tracked boards so future refreshes pull them directly (fresher,
        # canonical links). Confirm-or-nothing: only live boards get added.
        if (cfg["use_company_boards"] and cfg["auto_discover"]
                and cfg["discover_per_refresh"] > 0):
            from . import autodiscover
            _prog(running=True, phase="discover", done=0,
                  total=cfg["discover_per_refresh"],
                  detail="Scouting nearby companies…")

            def disc_prog(done, total, msg):
                _prog(done=done, total=max(total, 1), detail=msg)

            discovered = autodiscover.run(
                conn, agg_jobs, cfg["discover_per_refresh"], on_progress=disc_prog)
            for company, jobs in discovered:
                totals["found"] += len(jobs)
                sources_seen.update(j["source"] for j in jobs)
                _ingest(conn, jobs, company["id"], home, radius, budget, totals)
                conn.execute(
                    "UPDATE companies SET last_fetch_at = ?, last_error = NULL WHERE id = ?",
                    (db.now_iso(), company["id"]))
                conn.commit()
                if verbose:
                    print(f"  + discovered {company['name']} ({company['ats']}) "
                          f"found={len(jobs)}")
            totals["discovered"] = len(discovered)
            _prog(new=totals["new"], discovered=len(discovered))

    # --- Expire jobs that vanished from a healthy source --------------------
    # Only for sources that actually returned data this run; one blip must not
    # flag your whole board as expired.
    n_units = len(companies) + len(sites)
    if len(totals["errors"]) < max(1, n_units // 2):
        for src in sources_seen:
            db.mark_gone(conn, src, run_started)

    conn.execute(
        """UPDATE fetch_runs SET finished_at = ?, found = ?, new_jobs = ?, errors = ?
            WHERE id = ?""",
        (db.now_iso(), totals["found"], totals["new"],
         "\n".join(totals["errors"])[:2000] or None, run_id),
    )
    conn.commit()
    _prog(running=False, phase="done", detail="Done",
          new=totals["new"], found=totals["found"],
          discovered=totals["discovered"],
          errors=len(totals["errors"]))
    return totals
