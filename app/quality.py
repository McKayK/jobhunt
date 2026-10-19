"""Scam / spam / staffing-agency scoring.

Every job gets a risk_score (0..100) and a list of human-readable reasons,
so the UI can say *why* something looks off instead of silently hiding it.
Jobs at or above your threshold (default 60) are hidden from New/All and
shown under the "Flagged" tab, where you can still look.

Signals come from the FTC's job-scam guidance and university career-center
checklists (2025-26): off-platform chat interviews, paying for equipment or
training, check-forwarding and reshipping, task/commission schemes, personal
email domains, asking for SSN/bank details up front, pay far above normal for
low-skill remote work, vague postings, and employers with no web presence.

Postings from a company's own job board (Greenhouse, Workday, ...) or with
an apply link that points at one get a large trust bonus: scammers don't get
real ATS boards.

Staffing agencies aren't scams, just middlemen — they're flagged separately
(is_staffing) so you can hide them if you prefer applying direct.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

from .db import AGGREGATE_SOURCES

# (code, weight, label, pattern) — matched against title + description.
_TEXT_SIGNALS = [
    ("chat", 50, "Asks you to interview or chat on Telegram/WhatsApp/Signal",
     r"\b(telegram|whats\s?app|signal app|wickr|google hangouts?|kik messenger)\b"),
    ("text_apply", 30, "Says to apply by text message",
     r"\b(text|sms)\s+(us|me|your (name|resume))\b|\btext\s+[\"']?apply"),
    ("check", 55, "Mentions depositing a check or buying equipment with sent funds",
     r"(deposit (the |a )?check|check (to|for|will be sent to) (purchase|buy)|purchase (your |the )?(equipment|supplies) (with|from) (the )?(funds|check)|send (back )?the (remaining|balance))"),
    ("money_apps", 40, "Mentions payment by Zelle/Cash App/gift cards/crypto",
     r"\b(zelle|cash\s?app|venmo|gift\s?cards?|bitcoin|usdt|crypto(currency)? (wallet|payment|deposit))\b"),
    ("fees", 45, "Asks you to pay for training, equipment, or a starter kit",
     r"(training fee|starter kit|registration fee|pay (for )?(your own |the )?(training|background check|equipment|certification)|refundable (deposit|fee))"),
    ("reship", 55, "Package reshipping / forwarding from home",
     r"\b(reship(ping)?|re-ship(ping)?|package (forwarding|inspector|handler)s? .{0,30}(home|remote)|receive (and|&) (re)?ship packages|parcel forwarding)\b"),
    ("tasks", 45, "Pay-per-task / 'optimize apps to earn' scheme",
     r"(complete (simple |easy )?tasks (online )?(to|and) earn|optimi[sz]e (apps|products|data) (to|and) earn|commission per task|boost (app|product) ratings)"),
    ("pii", 35, "Asks for SSN or bank details before an offer",
     r"(social security (number|card)|\bssn\b|bank account (number|details|information)|routing number|copy of (your )?driver'?s license)"),
    ("no_interview", 20, "Promises hiring with no interview",
     r"\b(no interview(s)? (needed|required|necessary)|hired (immediately|on the spot) without)"),
    ("easy_money", 25, "'Earn $X per week/day' style pitch",
     r"(earn (up to )?\$\s?\d[\d,]{2,}\+? (per|a|/) ?(week|day)|make \$\s?\d[\d,]{2,}\+? (per|a|/) ?(week|day)|weekly pay of \$\s?\d)"),
    ("urgency", 8, "High-pressure urgency language",
     r"\b(urgent(ly)? hiring|immediate(ly)? start|start (today|tomorrow)|limited (spots|positions) available|apply now!!)"),
]
_TEXT_SIGNALS = [(c, w, l, re.compile(p, re.I)) for c, w, l, p in _TEXT_SIGNALS]

_SCAMMY_TITLES = re.compile(
    r"\b(data entry|personal assistant|virtual assistant|mystery shopper|package handler|"
    r"reshipping|chat moderator|product tester|envelope stuff|typist|survey taker|"
    r"payment processing (agent|clerk)|shipping (coordinator|clerk) .*remote|"
    r"secret shopper|online reviewer|app tester)\b", re.I)

_PERSONAL_EMAIL = re.compile(
    r"[a-z0-9._%+-]+@(gmail|yahoo|outlook|hotmail|aol|icloud|proton(mail)?|gmx|yandex|mail)\.(com|me|ru|net)\b", re.I)

_VAGUE_COMPANY = re.compile(
    r"^(unknown company|confidential|company confidential|hiring company|private company|"
    r"stealth( startup)?|anonymous|n/?a|various|our client|employer)$", re.I)

STAFFING = re.compile(
    r"\b(robert half|teksystems|insight global|kforce|aerotek|randstad|adecco|apex systems|"
    r"kelly services|manpower(group)?|experis|jobot|cybercoders|motion recruitment|modis|akkodis|"
    r"collabera|actalent|beacon hill|judge group|vaco|addison group|aston carter|allegis|"
    r"signature consultants|creative circle|hays recruitment|spherion|express employment|"
    r"volt (workforce|consulting)|yoh services|"
    r"infojini|staffing|recruit(ing|ers|ment)|talent (solutions|partners|group|acquisition partners)|"
    r"workforce solutions|employment (agency|services|solutions)|temps?|placement(s)?|"
    r"technical resources|it consulting services|consulting group llc)\b", re.I)
_STAFFING_TEXT = re.compile(
    r"\b(on behalf of (our|a) client|our client is (seeking|looking)|for (one of )?our clients?|"
    r"(w2|c2c|corp[- ]to[- ]corp) (only|contract)|contract[- ]to[- ]hire)\b", re.I)

_HOURLY = re.compile(r"\$\s?(\d{2,3}(?:\.\d+)?)\s*(?:[-–]\s*\$?\s?(\d{2,3}(?:\.\d+)?))?\s*(?:/|per\s)?\s*(?:hr|hour)", re.I)


_NEGATION = re.compile(
    r"\b(never|won'?t|don'?t|doesn'?t|do not|does not|will not|is not going to|"
    r"beware|scam(mer)?s?|fraud(ulent)?|phishing|impersonat\w*|fake)\b", re.I)


def _negated(text: str, start: int) -> bool:
    """Is this match inside an employer's anti-scam warning? ("We will never
    ask for your SSN", "beware of scammers using Telegram")."""
    sent_start = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    window = text[max(sent_start, start - 100):start]
    return bool(_NEGATION.search(window))


def assess(job: dict) -> tuple[int, list[dict], bool]:
    """Score one job dict. Returns (risk_score, flags, is_staffing).

    Works on fresh fetch dicts (which may carry extra '_' signals from the job
    sites) and on stored rows alike.
    """
    title = job.get("title") or ""
    desc = job.get("description") or ""
    company = (job.get("company_name") or "").strip()
    text = f"{title}\n{desc}"
    source = job.get("source") or ""
    flags: list[dict] = []

    def add(code, w, label):
        if not any(f["code"] == code for f in flags):
            flags.append({"code": code, "w": w, "label": label})

    for code, w, label, pat in _TEXT_SIGNALS:
        if any(not _negated(text, m.start()) for m in pat.finditer(text)):
            add(code, w, label)

    emails = list(job.get("_emails") or []) + [m.group(0) for m in _PERSONAL_EMAIL.finditer(desc)]
    if any(_PERSONAL_EMAIL.fullmatch(e.strip()) for e in emails):
        add("personal_email", 30, "Contact is a personal email (gmail/yahoo/…)")

    remote = bool(job.get("remote_hint") or job.get("remote")) or "remote" in (job.get("location_raw") or "").lower()
    if _SCAMMY_TITLES.search(title):
        add("scam_title", 20 + (10 if remote else 0),
            "Job title commonly used in scams" + (" (remote)" if remote else ""))
        sal = job.get("salary_text") or ""
        m = _HOURLY.search(sal) or _HOURLY.search(desc[:3000])
        if m and float(m.group(2) or m.group(1)) >= 35:
            add("pay_too_good", 30, "Pay is far above normal for this kind of role")

    if source in AGGREGATE_SOURCES:
        if not desc.strip():
            add("no_desc", 8, "No job description")
        elif len(desc.strip()) < 250:
            add("vague", 15, "Very short, vague description")
        if source == "indeed" and job.get("_company_profile") is False \
                and not job.get("company_url") and not job.get("_company_reviews"):
            add("no_presence", 15, "Employer has no website or reviews on Indeed")

    if _VAGUE_COMPANY.match(company):
        add("vague_company", 20, "Employer name is hidden or generic")

    if title and len(title) > 12 and title.upper() == title and re.search(r"[A-Z]{4}", title):
        add("caps", 8, "ALL-CAPS title")
    if "!!" in title or desc.count("!!!") >= 1:
        add("exclaim", 6, "Excessive exclamation marks")

    # --- Trust signals ---
    from .detect import detect_from_url
    on_board = job.get("company_id") is not None and source not in AGGREGATE_SOURCES
    apply_d = detect_from_url(job.get("apply_url") or "") if job.get("apply_url") else None
    if on_board:
        add("verified_board", -60, "Posted on the company's own job board")
    elif apply_d and apply_d.ats not in ("unknown",):
        add("ats_apply", -30, "Apply link goes to a real hiring system")
    reviews = job.get("_company_reviews")
    if reviews and reviews >= 20:
        add("reviewed", -15, f"Employer has {int(reviews)} reviews on Indeed")

    score = max(0, min(100, sum(f["w"] for f in flags)))
    staffing = bool(STAFFING.search(company)) or bool(_STAFFING_TEXT.search(desc[:4000]))
    return score, flags, staffing


def save(conn: sqlite3.Connection, job_hash: str, source: str, job: dict) -> None:
    """Store an assessment computed at fetch time (with the job-site extras)."""
    score, flags, staffing = assess(job)
    conn.execute(
        "UPDATE jobs SET risk_score=?, risk_flags=?, is_staffing=? WHERE job_hash=? AND source=?",
        (score, json.dumps(flags), 1 if staffing else 0, job_hash, source))


def mass_posting_pass(conn: sqlite3.Connection, threshold: int = 8) -> int:
    """Cross-job signal: the same company posting the same title in many
    places from job sites (typical of lead-gen and staffing spam). Adds or
    removes a 'mass' flag and re-totals the score."""
    rows = conn.execute(
        """SELECT company_name, LOWER(title) t, COUNT(DISTINCT LOWER(COALESCE(location_raw,''))) n
             FROM jobs WHERE gone_at IS NULL AND source IN (%s)
            GROUP BY company_name, LOWER(title) HAVING n >= ?""" %
        ",".join("?" * len(AGGREGATE_SOURCES)),
        (*sorted(AGGREGATE_SOURCES), threshold)).fetchall()
    spam = {(r["company_name"], r["t"]): r["n"] for r in rows}

    changed = 0
    cur = conn.execute(
        "SELECT id, company_name, title, risk_flags FROM jobs WHERE gone_at IS NULL AND (source IN (%s) OR risk_flags LIKE '%%\"mass\"%%')"
        % ",".join("?" * len(AGGREGATE_SOURCES)), tuple(sorted(AGGREGATE_SOURCES)))
    for r in cur.fetchall():
        flags = json.loads(r["risk_flags"] or "[]")
        had = any(f.get("code") == "mass" for f in flags)
        n = spam.get((r["company_name"], (r["title"] or "").lower()))
        if bool(n) == had:
            continue
        flags = [f for f in flags if f.get("code") != "mass"]
        if n:
            flags.append({"code": "mass", "w": 15, "label": f"Same posting copied to {n} locations"})
        score = max(0, min(100, sum(f.get("w", 0) for f in flags)))
        conn.execute("UPDATE jobs SET risk_flags=?, risk_score=? WHERE id=?",
                     (json.dumps(flags), score, r["id"]))
        changed += 1
    conn.commit()
    return changed


def backfill(conn: sqlite3.Connection, only_missing: bool = True) -> int:
    """Score stored jobs that predate the filter (no job-site extras available,
    so it's text-based only)."""
    where = "risk_flags IS NULL" if only_missing else "1=1"
    rows = conn.execute(f"SELECT * FROM jobs WHERE {where}").fetchall()
    for r in rows:
        score, flags, staffing = assess(dict(r))
        conn.execute("UPDATE jobs SET risk_score=?, risk_flags=?, is_staffing=? WHERE id=?",
                     (score, json.dumps(flags), 1 if staffing else 0, r["id"]))
    conn.commit()
    return len(rows)


def worst_label(flags_json: Optional[str]) -> Optional[str]:
    flags = json.loads(flags_json or "[]")
    pos = [f for f in flags if f.get("w", 0) > 0]
    return max(pos, key=lambda f: f["w"])["label"] if pos else None
