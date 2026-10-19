"""The refresh pipeline.

  1. Tracked company boards, fetched in parallel (the fast, accurate feed).
  2. Job sites (Indeed & co.) around your ZIP, plus — with remote_nationwide —
     the same keywords searched US-wide for remote-only jobs.
  3. Remote-only boards (Himalayas, Jobicy, Remotive, RemoteOK), each on its
     own polite minimum interval.
  4. Auto-discovery: job-site apply links that point at a company's own board
     get that company tracked (and fetched right away).
  5. Housekeeping: expire closed postings, scam/spam pass, resume match scores.

Keyword and radius filters are applied when you *view* the board, not here,
so changing them is instant. Foreign postings are dropped; too-far US jobs are
stored (widening your radius reveals them immediately).

Expiry, fixed in v3:
- A company board that fetched OK is complete, so its missing jobs closed.
  This is now per company: one company erroring no longer expires every
  other company on the same ATS.
- Job-site results are a capped, ranked sample, so absence proves nothing.
  They expire only after no search has returned them for N days (setting
  aggregate_expire_days). The old code expired them every run, which is why
  Indeed jobs kept vanishing.
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from . import db, geo, match, quality, settings
from .fetchers import aggregate, remote_boards
from .fetchers import ats as ats_fetchers

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


class Ctx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.cfg = settings.get_all(conn)
        self.home = geo.home_coords(conn)
        self.home_st = geo.home_state(conn)
        self.radius = self.cfg["radius_miles"]
        self.budget = geo.GeoBudget(40)
        self.tracked = db.company_by_norm(conn)
        self.totals = {"found": 0, "stored": 0, "new": 0, "companies": 0,
                       "errors": [], "dropped_foreign": 0, "unknown": 0,
                       "discovered": 0, "expired": 0}


def _ingest(ctx: Ctx, jobs: list[dict], company_id: int | None):
    """Geo-annotate, link to tracked companies, upsert, and score a batch."""
    conn, totals = ctx.conn, ctx.totals
    for job in jobs:
        r = geo.resolve_location(conn, job.get("location_raw"), home=ctx.home,
                                 radius=ctx.radius, budget=ctx.budget,
                                 home_st=ctx.home_st, remote_hint=bool(job.get("remote_hint")))
        job["geo_status"] = r["status"]
        job["distance_miles"] = r["distance_miles"]
        job["lat"], job["lon"] = r["lat"], r["lon"]
        job["country"] = r["country"]
        job["remote"] = r["status"] in ("remote", "remote_other_state")

        if r["status"] == "foreign":
            totals["dropped_foreign"] += 1
            continue
        if r["status"] == "unknown":
            totals["unknown"] += 1

        if company_id is None:
            hit = ctx.tracked.get(db.norm_company(job["company_name"]))
            job["company_id"] = hit["id"] if hit is not None else None
        else:
            job["company_id"] = company_id
        totals["stored"] += 1
        if db.upsert_job(conn, job):
            totals["new"] += 1
        quality.save(conn, db.job_hash(job["company_name"], job["title"], job.get("location_raw")),
                     job["source"], job)


def _phase_companies(ctx: Ctx, run_started: str, verbose: bool):
    conn, totals, cfg = ctx.conn, ctx.totals, ctx.cfg
    companies = db.active_companies(conn) if cfg["use_company_boards"] else []
    totals["companies"] = len(companies)
    if not companies:
        return
    _prog(running=True, phase="companies", done=0, total=len(companies),
          detail="Fetching company boards…")
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one_company, c): c for c in companies}
        done = 0
        for fut in as_completed(futures):
            company, jobs, err = fut.result()
            results.append((company, jobs, err))
            done += 1
            _prog(done=done, detail=f"Fetched {company['name']}"
                  + (" — ERROR" if err else f" ({len(jobs)} jobs)"))

    # DB writes stay on this thread; SQLite likes it that way.
    for company, jobs, err in results:
        if err:
            totals["errors"].append(f"{company['name']}: {err}")
            conn.execute(
                "UPDATE companies SET last_fetch_at = ?, last_error = ? WHERE id = ?",
                (db.now_iso(), err[:500], company["id"]))
        else:
            totals["found"] += len(jobs)
            _ingest(ctx, jobs, company["id"])
            totals["expired"] += db.mark_gone_company(conn, company["id"], run_started)
            conn.execute(
                "UPDATE companies SET last_fetch_at = ?, last_error = NULL WHERE id = ?",
                (db.now_iso(), company["id"]))
        conn.commit()
        _prog(new=totals["new"])
        if verbose:
            flag = f"  ERROR {err}" if err else ""
            print(f"  {company['name']:<28} found={len(jobs)}{flag}")


def _phase_job_sites(ctx: Ctx) -> list[dict]:
    cfg = ctx.cfg
    sites = [s for s, key in aggregate.SITE_SETTING_MAP.items() if cfg[key]]
    if not sites:
        return []
    total = aggregate.plan_size(sites, cfg["include_keywords"], cfg["remote_nationwide"])
    _prog(running=True, phase="aggregate", done=0, total=total, detail="Searching job sites…")
    done = [0]

    def on_progress(msg):
        done[0] += 1
        _prog(done=min(done[0], total), detail=msg)

    jobs, errors = aggregate.fetch_aggregate(
        sites=sites, home_zip=cfg["home_zip"], radius_miles=ctx.radius,
        search_terms=cfg["include_keywords"], results_wanted=cfg["results_per_site"],
        hours_old=cfg["aggregate_hours_old"], on_progress=on_progress,
        place_name=geo.zip_place_name(cfg["home_zip"]),
        remote_nationwide=cfg["remote_nationwide"],
    )
    ctx.totals["found"] += len(jobs)
    ctx.totals["errors"].extend(errors)
    _prog(detail=f"Processing {len(jobs)} postings…")
    _ingest(ctx, jobs, None)
    ctx.conn.commit()
    _prog(new=ctx.totals["new"])
    return jobs


def _board_due(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"board_last:{name}",)).fetchone()
    if not row:
        return True
    try:
        last = datetime.fromisoformat(row["value"])
    except Exception:
        return True
    hours = remote_boards.MIN_INTERVAL_HOURS.get(name, 2)
    return datetime.now(timezone.utc) - last >= timedelta(hours=hours)


def _phase_remote_boards(ctx: Ctx) -> list[dict]:
    cfg, conn = ctx.cfg, ctx.conn
    terms = [t for t in cfg["include_keywords"] if t.strip()]
    if not (cfg["remote_nationwide"] and cfg["use_remote_boards"] and terms):
        return []
    due = [n for n in remote_boards.BOARDS if _board_due(conn, n)]
    if not due:
        return []
    _prog(running=True, phase="remote", done=0, total=len(due), detail="Searching remote job boards…")
    all_jobs: list[dict] = []
    for i, name in enumerate(due):
        _prog(done=i, detail=f"Searching {name}…")
        try:
            jobs = remote_boards.BOARDS[name](terms)
            ctx.totals["found"] += len(jobs)
            all_jobs += jobs
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (f"board_last:{name}", datetime.now(timezone.utc).isoformat()))
        except Exception as e:
            ctx.totals["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
    _ingest(ctx, all_jobs, None)
    conn.commit()
    _prog(new=ctx.totals["new"])
    return all_jobs


def _phase_discover(ctx: Ctx, site_jobs: list[dict], verbose: bool):
    cfg, conn = ctx.cfg, ctx.conn
    if not (cfg["use_company_boards"] and cfg["auto_discover"]) or not site_jobs:
        return
    from . import autodiscover
    _prog(running=True, phase="discover", done=0, total=1, detail="Scouting company job boards…")

    def disc_prog(done, total, msg):
        _prog(done=done, total=max(total, 1), detail=msg)

    discovered = autodiscover.run(conn, site_jobs, cfg["discover_per_refresh"], on_progress=disc_prog)
    ctx.tracked = db.company_by_norm(conn)
    for company, jobs in discovered:
        ctx.totals["found"] += len(jobs)
        _ingest(ctx, jobs, company["id"])
        conn.execute(
            "UPDATE companies SET last_fetch_at = ?, last_error = NULL WHERE id = ?",
            (db.now_iso(), company["id"]))
        conn.commit()
        if verbose:
            print(f"  + discovered {company['name']} ({company['ats']}) found={len(jobs)}")
    ctx.totals["discovered"] = len(discovered)
    _prog(new=ctx.totals["new"], discovered=len(discovered))


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

    ctx = Ctx(conn)
    _phase_companies(ctx, run_started, verbose)
    site_jobs = _phase_job_sites(ctx)
    _phase_remote_boards(ctx)
    _phase_discover(ctx, site_jobs, verbose)

    # --- Housekeeping ---------------------------------------------------------
    _prog(running=True, phase="finishing", total=0, detail="Scoring jobs…")
    ctx.totals["expired"] += db.expire_aggregate(conn, ctx.cfg["aggregate_expire_days"])
    quality.mass_posting_pass(conn)
    match.rescore(conn, only_missing=True)

    totals = ctx.totals
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
