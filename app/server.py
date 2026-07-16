"""HTTP API + static frontend host."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, detect, geo, refresh as refresh_mod, settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_refresh_thread: Optional[threading.Thread] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(title="jobhunt", lifespan=lifespan)


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# --- Jobs ------------------------------------------------------------------

def _keyword_clauses(cfg: dict, where: list, params: list):
    """Query-time title keyword filters from settings.

    Excludes always win. Includes, when set, mean 'title must contain at
    least one of these'. Both are instant to change — no refetch.
    """
    for kw in cfg["exclude_keywords"]:
        where.append("LOWER(j.title) NOT LIKE ?")
        params.append(f"%{kw}%")
    if cfg["include_keywords"]:
        ors = " OR ".join(["LOWER(j.title) LIKE ?"] * len(cfg["include_keywords"]))
        where.append(f"({ors})")
        params.extend(f"%{kw}%" for kw in cfg["include_keywords"])


def _distance_clauses(cfg: dict, where: list, params: list):
    """Radius / remote / unknown handling, all from settings."""
    branches = ["(j.remote = 0 AND j.distance_miles IS NOT NULL AND j.distance_miles <= ?)"]
    params.append(cfg["radius_miles"])
    if cfg["include_remote"]:
        branches.append("j.remote = 1")
    if cfg["keep_unknown"]:
        branches.append("j.geo_status = 'unknown'")
        branches.append("(j.geo_status IS NULL AND j.distance_miles IS NULL AND j.remote = 0)")
    where.append("(" + " OR ".join(branches) + ")")
    where.append("(j.geo_status IS NULL OR j.geo_status != 'foreign')")


@app.get("/api/jobs")
def list_jobs(
    filter: str = Query("new", pattern="^(new|all|starred|applied|gone)$"),
    q: Optional[str] = None,
    company: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 500,
):
    conn = db.connect()
    try:
        cfg = settings.get_all(conn)

        # Keyword + distance filters shape what you're *discovering* (new/all).
        # Starred/applied/gone are things you already acted on — hiding them
        # because you later tweaked a keyword would be confusing.
        base_where: list[str] = ["1=1"]
        base_params: list = []
        if filter in ("new", "all"):
            _keyword_clauses(cfg, base_where, base_params)
            _distance_clauses(cfg, base_where, base_params)
        if q:
            base_where.append("(j.title LIKE ? OR j.company_name LIKE ?)")
            base_params += [f"%{q}%"] * 2
        if company:
            base_where.append("j.company_name = ?")
            base_params.append(company)
        if source:
            base_where.append("j.source = ?")
            base_params.append(source)

        tab_where = {
            "new":     "j.seen_at IS NULL AND j.hidden = 0 AND j.gone_at IS NULL",
            "all":     "j.hidden = 0 AND j.gone_at IS NULL",
            "starred": "j.starred = 1 AND j.gone_at IS NULL",
            "applied": "j.applied_at IS NOT NULL",
            "gone":    "j.gone_at IS NOT NULL",
        }

        sql = f"""
            SELECT j.*, c.ats
              FROM jobs j LEFT JOIN companies c ON c.id = j.company_id
             WHERE {' AND '.join(base_where)} AND {tab_where[filter]}
             ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC, j.id DESC
             LIMIT ?
        """
        jobs = rows_to_dicts(conn.execute(sql, base_params + [limit]).fetchall())

        counts = {}
        for key, clause in tab_where.items():
            w: list[str] = ["1=1"]
            p: list = []
            if key in ("new", "all"):
                _keyword_clauses(cfg, w, p)
                _distance_clauses(cfg, w, p)
            if q:
                w.append("(j.title LIKE ? OR j.company_name LIKE ?)")
                p += [f"%{q}%"] * 2
            if company:
                w.append("j.company_name = ?")
                p.append(company)
            if source:
                w.append("j.source = ?")
                p.append(source)
            counts["all_" if key == "all" else key] = conn.execute(
                f"SELECT COUNT(*) c FROM jobs j WHERE {' AND '.join(w)} AND {clause}",
                p).fetchone()["c"]

        last = conn.execute(
            "SELECT started_at, finished_at, new_jobs, errors FROM fetch_runs "
            "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()

        return {
            "jobs": jobs,
            "counts": counts,
            "last_run": dict(last) if last else None,
            "config": {
                "home_zip": cfg["home_zip"],
                "home_place": geo.zip_place_name(cfg["home_zip"]),
                "radius_miles": cfg["radius_miles"],
            },
        }
    finally:
        conn.close()


class JobAction(BaseModel):
    ids: list[int]
    value: bool = True


def _bulk_set(field_sql: str, ids: list[int], params_prefix: list = None):
    if not ids:
        return {"updated": 0}
    conn = db.connect()
    try:
        marks = ",".join("?" * len(ids))
        params = (params_prefix or []) + ids
        cur = conn.execute(f"UPDATE jobs SET {field_sql} WHERE id IN ({marks})", params)
        conn.commit()
        return {"updated": cur.rowcount}
    finally:
        conn.close()


@app.post("/api/jobs/seen")
def mark_seen(a: JobAction):
    return _bulk_set("seen_at = ?", a.ids, [db.now_iso() if a.value else None])


@app.post("/api/jobs/star")
def star(a: JobAction):
    return _bulk_set("starred = ?", a.ids, [1 if a.value else 0])


@app.post("/api/jobs/applied")
def applied(a: JobAction):
    ts = db.now_iso() if a.value else None
    if a.value:
        return _bulk_set("applied_at = ?, seen_at = COALESCE(seen_at, ?)", a.ids, [ts, ts])
    return _bulk_set("applied_at = NULL", a.ids)


@app.post("/api/jobs/hide")
def hide(a: JobAction):
    return _bulk_set("hidden = ?", a.ids, [1 if a.value else 0])


@app.post("/api/jobs/mark_all_seen")
def mark_all_seen():
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE jobs SET seen_at = ? WHERE seen_at IS NULL AND hidden = 0 AND gone_at IS NULL",
            (db.now_iso(),),
        )
        conn.commit()
        return {"updated": cur.rowcount}
    finally:
        conn.close()


# --- Settings ---------------------------------------------------------------

class SettingsIn(BaseModel):
    home_zip: Optional[str] = None
    radius_miles: Optional[float] = None
    include_remote: Optional[bool] = None
    keep_unknown: Optional[bool] = None
    include_keywords: Optional[list[str]] = None
    exclude_keywords: Optional[list[str]] = None
    use_company_boards: Optional[bool] = None
    use_indeed: Optional[bool] = None
    use_zip_recruiter: Optional[bool] = None
    use_google: Optional[bool] = None
    results_per_site: Optional[int] = None
    aggregate_hours_old: Optional[int] = None


@app.get("/api/settings")
def get_settings():
    conn = db.connect()
    try:
        cfg = settings.get_all(conn)
        cfg["home_place"] = geo.zip_place_name(cfg["home_zip"])
        return cfg
    finally:
        conn.close()


@app.put("/api/settings")
def put_settings(s: SettingsIn):
    conn = db.connect()
    try:
        updates = {k: v for k, v in s.model_dump().items() if v is not None}

        if "home_zip" in updates:
            z = str(updates["home_zip"]).strip()
            if geo.zip_place_name(z) is None:
                raise HTTPException(422, f"'{z}' doesn't look like a real US ZIP code.")

        old = settings.get_all(conn)
        try:
            cfg = settings.set_many(conn, updates)
        except ValueError as e:
            raise HTTPException(422, str(e))

        # ZIP or radius changed -> re-derive every stored distance instantly.
        recomputed = 0
        if (cfg["home_zip"] != old["home_zip"]
                or cfg["radius_miles"] != old["radius_miles"]):
            recomputed = geo.recompute_all(conn)

        cfg["home_place"] = geo.zip_place_name(cfg["home_zip"])
        cfg["recomputed"] = recomputed
        return cfg
    finally:
        conn.close()


# --- Companies -------------------------------------------------------------

class CompanyIn(BaseModel):
    name: str
    careers_url: Optional[str] = None
    ats: Optional[str] = None
    slug: Optional[str] = None


@app.get("/api/companies")
def list_companies():
    conn = db.connect()
    try:
        rows = conn.execute("""
            SELECT c.*, COUNT(j.id) AS job_count
              FROM companies c LEFT JOIN jobs j
                ON j.company_id = c.id AND j.gone_at IS NULL
             WHERE c.active = 1
             GROUP BY c.id ORDER BY c.name
        """).fetchall()
        return {"companies": rows_to_dicts(rows)}
    finally:
        conn.close()


@app.post("/api/companies/detect")
def detect_company(c: CompanyIn):
    d = detect.detect(c.name, c.careers_url, probe=True)
    return {
        "name": c.name, "ats": d.ats, "slug": d.slug,
        "workday_host": d.workday_host, "workday_path": d.workday_path,
        "confidence": d.confidence, "note": d.note,
        "supported": d.ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "workday"),
    }


@app.post("/api/companies")
def add_company(c: CompanyIn):
    ats, slug = c.ats, c.slug
    note = ""
    wd_host = wd_path = None
    if not ats or not slug:
        d = detect.detect(c.name, c.careers_url, probe=True)
        ats, slug, note = d.ats, d.slug, d.note
        wd_host, wd_path = d.workday_host, d.workday_path
    if not slug or ats == "unknown":
        raise HTTPException(
            422,
            f"Couldn't detect a supported job board for '{c.name}'. "
            "Try pasting their careers page URL — or skip it: the Indeed/"
            "ZipRecruiter search already covers companies not on your list.")

    conn = db.connect()
    try:
        cid = db.add_company(conn, c.name, c.careers_url, ats, slug, wd_host, wd_path)
        return {"id": cid, "name": c.name, "ats": ats, "slug": slug, "note": note}
    finally:
        conn.close()


@app.delete("/api/companies/{cid}")
def remove_company(cid: int):
    conn = db.connect()
    try:
        conn.execute("UPDATE companies SET active = 0 WHERE id = ?", (cid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# --- Refresh (background, with live progress) --------------------------------

@app.post("/api/refresh")
def start_refresh():
    global _refresh_thread
    if _refresh_thread is not None and _refresh_thread.is_alive():
        raise HTTPException(409, "A refresh is already running.")

    def run():
        conn = db.connect()
        try:
            refresh_mod.refresh_all(conn, verbose=False)
        except Exception as e:
            refresh_mod._prog(running=False, phase="error",
                              detail=f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            conn.close()

    refresh_mod._prog(running=True, phase="starting", done=0, total=0,
                      detail="Starting…", new=0)
    _refresh_thread = threading.Thread(target=run, daemon=True)
    _refresh_thread.start()
    return {"started": True}


@app.get("/api/refresh/status")
def refresh_status():
    return dict(refresh_mod.PROGRESS)


@app.get("/api/health")
def health():
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        return {"ok": True, "jobs": n}
    finally:
        conn.close()


# --- Static frontend -------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
