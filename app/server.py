"""HTTP API + static frontend host."""
from __future__ import annotations

import base64
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, boardfinder, db, detect, geo, match, quality, refresh as refresh_mod, resume, settings
from .fetchers import ats as ats_fetchers

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SEED_PATH = Path(__file__).resolve().parent / "seed_companies.json"

_refresh_thread: Optional[threading.Thread] = None
APP_STATUSES = ("applied", "interviewing", "offer", "rejected", "withdrawn")


def import_seed_companies(conn) -> int:
    """Load the bundled starter list (Utah County employers with verified
    boards). Safe to run repeatedly — existing companies are just revived."""
    data = json.loads(SEED_PATH.read_text())
    for c in data:
        db.add_company(conn, c["name"], c.get("careers_url"), c["ats"], c.get("slug"),
                       c.get("workday_host"), c.get("workday_path"),
                       board_url=ats_fetchers.board_url(c["ats"], c.get("slug"),
                                                        c.get("workday_host"), c.get("workday_path")),
                       added_by="seed")
    return len(data)


def _startup_migrations(conn) -> None:
    """One-time upgrade work for databases from older versions."""
    done = conn.execute("SELECT value FROM meta WHERE key='migrated:v3'").fetchone()
    if not done:
        # Repair Workday rows the old add_company could never fix (e.g. Domo
        # stuck on the wrong wd5 host) using the verified starter list.
        for c in json.loads(SEED_PATH.read_text()):
            if c["ats"] == "workday":
                conn.execute(
                    """UPDATE companies SET workday_host = ?, workday_path = ?, careers_url = ?
                        WHERE ats = 'workday' AND slug = ? AND last_error IS NOT NULL""",
                    (c["workday_host"], c["workday_path"], c.get("careers_url"), c["slug"]))
        try:
            geo.recompute_all(conn)          # fixes 'unknown' rows + remote eligibility
        except Exception:
            pass
        quality.backfill(conn, only_missing=False)
        match.rescore(conn)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated:v3', ?)", (db.now_iso(),))
        conn.commit()
    if conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"] == 0:
        import_seed_companies(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    _startup_migrations(conn)
    conn.close()
    yield


app = FastAPI(title="jobhunt", lifespan=lifespan)


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _conn():
    return db.connect()


# --- Jobs ------------------------------------------------------------------

def _keyword_clauses(cfg: dict, where: list, params: list):
    """Title keyword filters, matched on word boundaries (see db.kw_match)."""
    for kw in cfg["exclude_keywords"]:
        where.append("KW(j.title, ?) = 0")
        params.append(kw)
    if cfg["include_keywords"]:
        where.append("(" + " OR ".join(["KW(j.title, ?) = 1"] * len(cfg["include_keywords"])) + ")")
        params.extend(cfg["include_keywords"])


def _distance_clauses(cfg: dict, where: list, params: list):
    """Radius / remote / unknown handling, all from settings."""
    branches = ["(j.remote = 0 AND j.distance_miles IS NOT NULL AND j.distance_miles <= ?)"]
    params.append(cfg["radius_miles"])
    if cfg["include_remote"]:
        branches.append("(j.remote = 1 AND COALESCE(j.geo_status,'remote') = 'remote')")
    if cfg["keep_unknown"]:
        branches.append("j.geo_status = 'unknown'")
        branches.append("(j.geo_status IS NULL AND j.distance_miles IS NULL AND j.remote = 0)")
    where.append("(" + " OR ".join(branches) + ")")
    where.append("(j.geo_status IS NULL OR j.geo_status != 'foreign')")


def _quality_clauses(cfg: dict, where: list, params: list, risky: bool = False):
    if cfg["blocked_companies"]:
        where.append("LOWER(TRIM(j.company_name)) NOT IN (%s)" % ",".join("?" * len(cfg["blocked_companies"])))
        params.extend(cfg["blocked_companies"])
    if risky:
        where.append("j.risk_score >= ?")
        params.append(cfg["risk_threshold"])
        return
    if cfg["hide_risky"]:
        where.append("j.risk_score < ?")
        params.append(cfg["risk_threshold"])
    if cfg["hide_staffing"]:
        where.append("j.is_staffing = 0")


TABS = {
    # name: (where, discovery filters?, risky-only?)
    "new":          ("j.seen_at IS NULL AND j.hidden = 0 AND j.gone_at IS NULL", True, False),
    "all":          ("j.hidden = 0 AND j.gone_at IS NULL", True, False),
    "starred":      ("j.starred = 1", False, False),
    "applications": ("(j.applied_at IS NOT NULL OR j.app_status IS NOT NULL)", False, False),
    "recent":       ("j.seen_at IS NOT NULL AND j.seen_at >= datetime('now', '-30 days')", False, False),
    "flagged":      ("j.hidden = 0 AND j.gone_at IS NULL", True, True),
    "hidden":       ("j.hidden = 1", False, False),
    "gone":         ("j.gone_at IS NOT NULL", False, False),
    "everything":   ("1=1", False, False),
}
ORDERS = {
    "newest": "COALESCE(j.posted_at, j.first_seen_at) DESC, j.id DESC",
    "match": "COALESCE(j.match_score, -1) DESC, COALESCE(j.posted_at, j.first_seen_at) DESC",
    "seen": "j.seen_at DESC",
    "status": "COALESCE(j.status_at, j.applied_at) DESC",
    "distance": "j.remote ASC, COALESCE(j.distance_miles, 9999) ASC",
}

LIST_COLS = """j.id, j.company_id, j.company_name, j.title, j.location_raw, j.distance_miles,
    j.geo_status, j.remote, j.url, j.apply_url, j.company_url, j.salary_text, j.job_type,
    j.posted_at, j.source, j.first_seen_at, j.last_seen_at, j.gone_at, j.risk_score,
    j.risk_flags, j.is_staffing, j.match_score, j.seen_at, j.applied_at, j.app_status,
    j.status_at, j.hidden, j.starred, (j.notes IS NOT NULL AND j.notes != '') AS has_notes,
    c.ats AS company_ats, c.board_url AS company_board"""


def _where_for(tab: str, cfg: dict, q: Optional[str], company: Optional[str],
               source: Optional[str]) -> tuple[list[str], list]:
    clause, discovery, risky = TABS[tab]
    where, params = [clause], []
    if discovery:
        _keyword_clauses(cfg, where, params)
        _distance_clauses(cfg, where, params)
        _quality_clauses(cfg, where, params, risky=risky)
    if q:
        where.append("(j.title LIKE ? OR j.company_name LIKE ? OR j.location_raw LIKE ?)")
        params += [f"%{q}%"] * 3
    if company:
        where.append("j.company_name = ?")
        params.append(company)
    if source:
        where.append("j.source = ?")
        params.append(source)
    return where, params


@app.get("/api/jobs")
def list_jobs(
    filter: str = Query("new", pattern="^(" + "|".join(list(TABS) + ["applied"]) + ")$"),
    q: Optional[str] = None,
    company: Optional[str] = None,
    source: Optional[str] = None,
    sort: Optional[str] = None,
    limit: int = 500,
):
    if filter == "applied":          # old name for the Applications tab
        filter = "applications"
    if sort is not None and sort not in ORDERS:
        raise HTTPException(422, f"sort must be one of {list(ORDERS)}")
    conn = _conn()
    try:
        cfg = settings.get_all(conn)
        where, params = _where_for(filter, cfg, q, company, source)
        order = ORDERS[sort or {"recent": "seen", "applications": "status"}.get(filter, "newest")]
        sql = f"""SELECT {LIST_COLS}
                    FROM jobs j LEFT JOIN companies c ON c.id = j.company_id
                   WHERE {' AND '.join(where)}
                   ORDER BY {order} LIMIT ?"""
        jobs = rows_to_dicts(conn.execute(sql, params + [limit]).fetchall())
        for j in jobs:
            j["risk_label"] = quality.worst_label(j.pop("risk_flags"))

        counts = {}
        for key in TABS:
            if key in ("everything",):
                continue
            w, p = _where_for(key, cfg, q, company, source)
            counts["all_" if key == "all" else key] = conn.execute(
                f"SELECT COUNT(*) c FROM jobs j WHERE {' AND '.join(w)}", p).fetchone()["c"]

        last = conn.execute(
            "SELECT started_at, finished_at, new_jobs, errors FROM fetch_runs "
            "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        has_resume = conn.execute("SELECT 1 FROM resumes LIMIT 1").fetchone() is not None

        return {
            "jobs": jobs,
            "counts": counts,
            "last_run": dict(last) if last else None,
            "config": {
                "home_zip": cfg["home_zip"],
                "home_place": geo.zip_place_name(cfg["home_zip"]),
                "radius_miles": cfg["radius_miles"],
                "risk_threshold": cfg["risk_threshold"],
                "has_resume": has_resume,
                "ai": ai.provider(conn),
            },
        }
    finally:
        conn.close()


@app.get("/api/jobs/{jid}")
def job_detail(jid: int, fetch: bool = True):
    conn = _conn()
    try:
        row = conn.execute(
            """SELECT j.*, c.ats AS company_ats, c.board_url AS company_board, c.slug AS company_slug,
                      c.careers_url AS company_careers, c.workday_host, c.workday_path
                 FROM jobs j LEFT JOIN companies c ON c.id = j.company_id WHERE j.id = ?""",
            (jid,)).fetchone()
        if not row:
            raise HTTPException(404, "No such job")
        job = dict(row)
        if job.get("company_ats") and not job.get("company_board"):
            job["company_board"] = ats_fetchers.board_url(job["company_ats"], job.get("company_slug"),
                                                          job.get("workday_host"), job.get("workday_path"))
        if fetch and not (job.get("description") or "").strip():
            desc = ats_fetchers.fetch_description(row, row)
            if desc:
                job["description"] = desc
                conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (desc, jid))
                _rescore_one(conn, jid)
                conn.commit()
                job.update(dict(conn.execute(
                    "SELECT match_score, match_detail FROM jobs WHERE id=?", (jid,)).fetchone()))
        job["risk_flags"] = db.loads(job.get("risk_flags"), [])
        job["match_detail"] = db.loads(job.get("match_detail"), None)
        job["tailored"] = rows_to_dicts(conn.execute(
            "SELECT t.id, t.created_at, t.model, r.name AS resume_name FROM tailored t "
            "LEFT JOIN resumes r ON r.id = t.resume_id WHERE t.job_id = ? ORDER BY t.id DESC",
            (jid,)).fetchall())
        return job
    finally:
        conn.close()


def _rescore_one(conn, jid: int):
    text = match.default_resume_text(conn)
    r = conn.execute("SELECT title, description FROM jobs WHERE id=?", (jid,)).fetchone()
    if text and r:
        s, d = match.score(match.skills_in(text), r["title"], r["description"])
        conn.execute("UPDATE jobs SET match_score=?, match_detail=? WHERE id=?", (s, json.dumps(d), jid))


class JobAction(BaseModel):
    ids: list[int]
    value: bool = True


def _bulk_set(field_sql: str, ids: list[int], params_prefix: list = None):
    if not ids:
        return {"updated": 0}
    conn = _conn()
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
    # Seen only ever moves forward on the first open, so 'Recent' keeps a
    # stable "when did I first look at this" order.
    if a.value:
        return _bulk_set("seen_at = COALESCE(seen_at, ?)", a.ids, [db.now_iso()])
    return _bulk_set("seen_at = NULL", a.ids)


@app.post("/api/jobs/star")
def star(a: JobAction):
    return _bulk_set("starred = ?", a.ids, [1 if a.value else 0])


@app.post("/api/jobs/applied")
def applied(a: JobAction):
    ts = db.now_iso()
    if a.value:
        return _bulk_set("applied_at = COALESCE(applied_at, ?), seen_at = COALESCE(seen_at, ?), "
                         "app_status = COALESCE(app_status, 'applied'), status_at = COALESCE(status_at, ?)",
                         a.ids, [ts, ts, ts])
    return _bulk_set("applied_at = NULL, app_status = NULL, status_at = NULL", a.ids)


@app.post("/api/jobs/hide")
def hide(a: JobAction):
    return _bulk_set("hidden = ?, seen_at = COALESCE(seen_at, ?)", a.ids,
                     [1 if a.value else 0, db.now_iso()])


@app.post("/api/jobs/mark_all_seen")
def mark_all_seen():
    conn = _conn()
    try:
        cfg = settings.get_all(conn)
        where, params = _where_for("new", cfg, None, None, None)
        cur = conn.execute(
            f"UPDATE jobs SET seen_at = ? WHERE id IN (SELECT j.id FROM jobs j WHERE {' AND '.join(where)})",
            [db.now_iso()] + params)
        conn.commit()
        return {"updated": cur.rowcount}
    finally:
        conn.close()


class StatusIn(BaseModel):
    status: Optional[str] = None


@app.post("/api/jobs/{jid}/status")
def set_status(jid: int, s: StatusIn):
    st = (s.status or "").strip().lower() or None
    if st and st not in APP_STATUSES:
        raise HTTPException(422, f"status must be one of {APP_STATUSES}")
    conn = _conn()
    try:
        ts = db.now_iso()
        if st:
            conn.execute("""UPDATE jobs SET app_status = ?, status_at = ?,
                                   applied_at = COALESCE(applied_at, ?), seen_at = COALESCE(seen_at, ?)
                             WHERE id = ?""", (st, ts, ts, ts, jid))
        else:
            conn.execute("UPDATE jobs SET app_status = NULL, status_at = NULL, applied_at = NULL WHERE id = ?",
                         (jid,))
        conn.commit()
        return {"ok": True, "status": st}
    finally:
        conn.close()


class TextIn(BaseModel):
    text: Optional[str] = None


@app.put("/api/jobs/{jid}/notes")
def set_notes(jid: int, t: TextIn):
    conn = _conn()
    try:
        conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", ((t.text or "").strip() or None, jid))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.put("/api/jobs/{jid}/description")
def set_description(jid: int, t: TextIn):
    """For postings whose text we couldn't fetch: paste it in, so matching
    and tailoring have something to work with."""
    conn = _conn()
    try:
        conn.execute("UPDATE jobs SET description = ? WHERE id = ?", ((t.text or "").strip() or None, jid))
        _rescore_one(conn, jid)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


class BlockIn(BaseModel):
    company: str
    value: bool = True


@app.post("/api/block")
def block_company(b: BlockIn):
    conn = _conn()
    try:
        name = b.company.strip().lower()
        cur = settings.get(conn, "blocked_companies")
        new = [c for c in cur if c != name] + ([name] if b.value else [])
        settings.set_many(conn, {"blocked_companies": new})
        return {"blocked_companies": new}
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
    use_linkedin: Optional[bool] = None
    use_glassdoor: Optional[bool] = None
    remote_nationwide: Optional[bool] = None
    use_remote_boards: Optional[bool] = None
    results_per_site: Optional[int] = None
    aggregate_hours_old: Optional[int] = None
    aggregate_expire_days: Optional[int] = None
    auto_discover: Optional[bool] = None
    discover_per_refresh: Optional[int] = None
    hide_risky: Optional[bool] = None
    risk_threshold: Optional[int] = None
    hide_staffing: Optional[bool] = None
    blocked_companies: Optional[list[str]] = None
    ai_provider: Optional[str] = None


@app.get("/api/settings")
def get_settings():
    conn = _conn()
    try:
        cfg = settings.get_all(conn)
        cfg["home_place"] = geo.zip_place_name(cfg["home_zip"])
        return cfg
    finally:
        conn.close()


@app.put("/api/settings")
def put_settings(s: SettingsIn):
    conn = _conn()
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


# --- AI keys / status ----------------------------------------------------------

class KeysIn(BaseModel):
    anthropic_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    ai_provider: Optional[str] = None


@app.get("/api/ai/status")
def ai_status():
    conn = _conn()
    try:
        return ai.status(conn)
    finally:
        conn.close()


@app.put("/api/ai/keys")
def ai_keys(k: KeysIn):
    """Keys are write-only: stored server-side, never sent back."""
    conn = _conn()
    try:
        if k.anthropic_api_key is not None:
            settings.set_secret(conn, "anthropic_api_key", k.anthropic_api_key)
        if k.gemini_api_key is not None:
            settings.set_secret(conn, "gemini_api_key", k.gemini_api_key)
        if k.ai_provider:
            settings.set_many(conn, {"ai_provider": k.ai_provider})
        return ai.status(conn)
    finally:
        conn.close()


# --- Resumes --------------------------------------------------------------------

class ResumeIn(BaseModel):
    name: Optional[str] = None
    filename: Optional[str] = None
    data_base64: Optional[str] = None
    text: Optional[str] = None
    is_default: Optional[bool] = None


@app.get("/api/resumes")
def list_resumes():
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name, filename, is_default, created_at, updated_at, LENGTH(text) AS chars "
            "FROM resumes ORDER BY is_default DESC, updated_at DESC").fetchall()
        out = rows_to_dicts(rows)
        for r in out:
            t = conn.execute("SELECT text FROM resumes WHERE id=?", (r["id"],)).fetchone()["text"]
            r["skills"] = sorted(match.skills_in(t))
        return {"resumes": out}
    finally:
        conn.close()


@app.get("/api/resumes/{rid}")
def get_resume(rid: int):
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM resumes WHERE id=?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "No such resume")
        return dict(r)
    finally:
        conn.close()


def _set_default(conn, rid: int):
    conn.execute("UPDATE resumes SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (rid,))


@app.post("/api/resumes")
def add_resume(r: ResumeIn):
    text = (r.text or "").strip()
    if r.data_base64:
        try:
            raw = base64.b64decode(r.data_base64.split(",")[-1])
            text = resume.extract_text(r.filename or "resume.txt", raw)
        except Exception as e:
            raise HTTPException(422, f"Couldn't read that file: {type(e).__name__}")
    if len(text) < 100:
        raise HTTPException(422, "That resume came out nearly empty. If it's a scanned PDF, "
                                 "paste the text instead.")
    conn = _conn()
    try:
        first = conn.execute("SELECT COUNT(*) c FROM resumes").fetchone()["c"] == 0
        cur = conn.execute(
            "INSERT INTO resumes (name, filename, text, is_default) VALUES (?,?,?,?) RETURNING id",
            ((r.name or r.filename or "My resume").strip(), r.filename, text, 1 if first else 0))
        rid = cur.fetchone()["id"]
        if r.is_default:
            _set_default(conn, rid)
        conn.commit()
        n = match.rescore(conn)
        return {"id": rid, "chars": len(text), "skills": sorted(match.skills_in(text)), "rescored": n}
    finally:
        conn.close()


@app.put("/api/resumes/{rid}")
def update_resume(rid: int, r: ResumeIn):
    conn = _conn()
    try:
        if r.name is not None:
            conn.execute("UPDATE resumes SET name=?, updated_at=datetime('now') WHERE id=?", (r.name.strip(), rid))
        if r.text is not None:
            conn.execute("UPDATE resumes SET text=?, updated_at=datetime('now') WHERE id=?", (r.text.strip(), rid))
        if r.is_default:
            _set_default(conn, rid)
        conn.commit()
        if r.text is not None or r.is_default:
            match.rescore(conn)
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/resumes/{rid}")
def delete_resume(rid: int):
    conn = _conn()
    try:
        conn.execute("DELETE FROM resumes WHERE id=?", (rid,))
        if not conn.execute("SELECT 1 FROM resumes WHERE is_default=1").fetchone():
            r = conn.execute("SELECT id FROM resumes ORDER BY updated_at DESC LIMIT 1").fetchone()
            if r:
                _set_default(conn, r["id"])
        conn.commit()
        match.rescore(conn)
        return {"ok": True}
    finally:
        conn.close()


# --- Tailoring ------------------------------------------------------------------

class TailorIn(BaseModel):
    resume_id: Optional[int] = None
    cover_letter: bool = False


@app.post("/api/jobs/{jid}/tailor")
def tailor_job(jid: int, t: TailorIn):
    conn = _conn()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not job:
            raise HTTPException(404, "No such job")
        if t.resume_id:
            res = conn.execute("SELECT * FROM resumes WHERE id=?", (t.resume_id,)).fetchone()
        else:
            res = conn.execute("SELECT * FROM resumes ORDER BY is_default DESC, updated_at DESC LIMIT 1").fetchone()
        if not res:
            raise HTTPException(422, "Add your resume first (Resume button, top right).")
        jd = dict(job)
        if not (jd.get("description") or "").strip():
            comp = conn.execute("SELECT * FROM companies WHERE id=?", (jd.get("company_id"),)).fetchone()
            desc = ats_fetchers.fetch_description(job, comp)
            if desc:
                jd["description"] = desc
                conn.execute("UPDATE jobs SET description=? WHERE id=?", (desc, jid))
                conn.commit()
        try:
            content, model = ai.tailor(conn, res["text"], jd, cover_letter=t.cover_letter)
        except ai.AIError as e:
            raise HTTPException(422, str(e))
        cur = conn.execute("INSERT INTO tailored (job_id, resume_id, model, content) VALUES (?,?,?,?) RETURNING id",
                           (jid, res["id"], model, json.dumps(content)))
        tid = cur.fetchone()["id"]
        conn.commit()
        return {"id": tid, "model": model, "content": content}
    finally:
        conn.close()


def _tailored(conn, tid: int):
    row = conn.execute(
        "SELECT t.*, j.company_name, j.title FROM tailored t JOIN jobs j ON j.id = t.job_id WHERE t.id=?",
        (tid,)).fetchone()
    if not row:
        raise HTTPException(404, "No such tailored resume")
    return row, json.loads(row["content"])


@app.get("/api/tailored/{tid}")
def get_tailored(tid: int):
    conn = _conn()
    try:
        row, content = _tailored(conn, tid)
        return {"id": tid, "job_id": row["job_id"], "model": row["model"],
                "created_at": row["created_at"], "content": content}
    finally:
        conn.close()


@app.get("/api/tailored/{tid}/resume.docx")
def tailored_docx(tid: int):
    conn = _conn()
    try:
        row, content = _tailored(conn, tid)
        r = content.get("resume") or {}
        name = resume.safe_filename(r.get("name"), "Resume", row["company_name"])
        return Response(resume.to_docx(r), headers={"Content-Disposition": f'attachment; filename="{name}.docx"'},
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    finally:
        conn.close()


@app.get("/api/tailored/{tid}/resume.txt")
def tailored_txt(tid: int):
    conn = _conn()
    try:
        _, content = _tailored(conn, tid)
        return PlainTextResponse(resume.to_text(content.get("resume") or {}))
    finally:
        conn.close()


@app.get("/api/tailored/{tid}/cover.docx")
def tailored_cover(tid: int):
    conn = _conn()
    try:
        row, content = _tailored(conn, tid)
        text = content.get("cover_letter") or ""
        if not text.strip():
            raise HTTPException(404, "No cover letter was generated for this version.")
        name = resume.safe_filename((content.get("resume") or {}).get("name"), "Cover_Letter", row["company_name"])
        return Response(resume.cover_letter_docx(text), headers={"Content-Disposition": f'attachment; filename="{name}.docx"'},
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    finally:
        conn.close()


# --- Companies -------------------------------------------------------------

class CompanyIn(BaseModel):
    name: str
    careers_url: Optional[str] = None
    ats: Optional[str] = None
    slug: Optional[str] = None
    workday_host: Optional[str] = None
    workday_path: Optional[str] = None
    board_url: Optional[str] = None


@app.get("/api/companies")
def list_companies():
    conn = _conn()
    try:
        rows = conn.execute("""
            SELECT c.*,
                   (SELECT COUNT(*) FROM jobs j WHERE j.company_id = c.id AND j.gone_at IS NULL) AS job_count,
                   (SELECT COUNT(*) FROM jobs j WHERE j.company_id = c.id AND j.gone_at IS NULL
                       AND (j.geo_status = 'in_range' OR j.geo_status = 'remote')) AS near_count
              FROM companies c
             WHERE c.active = 1
             ORDER BY LOWER(c.name)
        """).fetchall()
        out = rows_to_dicts(rows)
        for c in out:
            c["board_url"] = c["board_url"] or ats_fetchers.board_url(
                c["ats"], c["slug"], c["workday_host"], c["workday_path"]) or c["careers_url"]
        return {"companies": out}
    finally:
        conn.close()


@app.post("/api/companies/find")
def find_company_board(c: CompanyIn):
    """'Go find this company's job board and give me the link.'"""
    if not c.name.strip():
        raise HTTPException(422, "Enter a company name.")
    conn = _conn()
    try:
        return boardfinder.find_board(conn, c.name, c.careers_url)
    finally:
        conn.close()


@app.post("/api/companies/detect")
def detect_company(c: CompanyIn):
    d = detect.detect(c.name, c.careers_url, probe=True)
    return {"name": c.name, "ats": d.ats, "slug": d.slug, "workday_host": d.workday_host,
            "workday_path": d.workday_path, "confidence": d.confidence, "note": d.note,
            "board_url": d.url, "supported": d.supported}


@app.post("/api/companies")
def add_company(c: CompanyIn):
    """Track a company. Either pass ats+slug (e.g. from the finder), or a
    name + careers URL to detect. ats='link' saves a bookmark-only company."""
    ats, slug = c.ats, c.slug
    wd_host, wd_path, note, url = c.workday_host, c.workday_path, "", c.board_url
    if ats == "link":
        slug = slug or detect.norm_company(c.name) or c.name.strip().lower()
        url = url or c.careers_url
        if not url:
            raise HTTPException(422, "A link-only company needs a careers URL.")
    elif not ats or not (slug or wd_host):
        d = detect.detect(c.name, c.careers_url, probe=True)
        if d.ats == "link" and d.url:
            ats, slug, url, note = "link", detect.norm_company(c.name), d.url, d.note
        else:
            ats, slug, note, url = d.ats, d.slug, d.note, d.url
            wd_host, wd_path = d.workday_host, d.workday_path
    if not slug or ats in (None, "unknown"):
        raise HTTPException(
            422,
            f"Couldn't detect a supported job board for '{c.name}'. Try 'Find job board' — "
            "it also checks their postings' apply links and searches the web.")

    conn = _conn()
    try:
        cid = db.add_company(conn, c.name.strip(), c.careers_url or url, ats, slug, wd_host, wd_path,
                             board_url=url or ats_fetchers.board_url(ats, slug, wd_host, wd_path),
                             added_by="manual")
        # Link existing job-site postings to it.
        norm = db.norm_company(c.name)
        for r in conn.execute("SELECT id, company_name FROM jobs WHERE company_id IS NULL").fetchall():
            if db.norm_company(r["company_name"]) == norm:
                conn.execute("UPDATE jobs SET company_id = ? WHERE id = ?", (cid, r["id"]))
        conn.commit()
        return {"id": cid, "name": c.name, "ats": ats, "slug": slug, "note": note, "board_url": url}
    finally:
        conn.close()


@app.post("/api/companies/import-starter")
def import_starter():
    conn = _conn()
    try:
        return {"imported": import_seed_companies(conn)}
    finally:
        conn.close()


@app.delete("/api/companies/{cid}")
def remove_company(cid: int):
    conn = _conn()
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
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        return {"ok": True, "jobs": n}
    finally:
        conn.close()


# --- Static frontend -------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
