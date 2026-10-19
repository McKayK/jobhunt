"""Optional AI: resume tailoring and the last-resort step of "Find job board".

Two providers, plain HTTPS (no SDKs to install):
- Anthropic (Claude) — set ANTHROPIC_API_KEY or paste a key in Settings.
  Default model claude-sonnet-5-5 (~4-5 cents per tailored resume).
- Google Gemini — set GEMINI_API_KEY or paste a key. Has a free tier.

ai_provider = auto picks Claude if a Claude key is set, else Gemini.

Everything the AI writes about you is constrained to facts already in your
resume: the prompt forbids inventing employers, titles, dates, degrees,
numbers or skills, and the result is shown to you (with a list of what was
changed) before you use it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

import requests

from . import config, settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class AIError(RuntimeError):
    pass


def provider(conn: sqlite3.Connection) -> Optional[str]:
    pref = settings.get(conn, "ai_provider")
    have_a = bool(settings.get_secret(conn, "anthropic_api_key"))
    have_g = bool(settings.get_secret(conn, "gemini_api_key"))
    if pref == "anthropic":
        return "anthropic" if have_a else None
    if pref == "gemini":
        return "gemini" if have_g else None
    return "anthropic" if have_a else ("gemini" if have_g else None)


def status(conn: sqlite3.Connection) -> dict:
    p = provider(conn)
    return {
        "provider": p,
        "model": (config.ANTHROPIC_MODEL if p == "anthropic"
                  else config.GEMINI_MODEL if p == "gemini" else None),
        "keys": settings.secrets_status(conn),
        "preference": settings.get(conn, "ai_provider"),
    }


def complete(conn: sqlite3.Connection, system: str, user: str, max_tokens: int = 4000,
             web_search: bool = False, timeout: float = 120) -> tuple[str, str]:
    """Run one prompt. Returns (text, model_used). Raises AIError."""
    p = provider(conn)
    if not p:
        raise AIError("No AI key configured. Add a Claude or Gemini API key in Resume → AI settings.")

    if p == "anthropic":
        body: dict[str, Any] = {
            "model": config.ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if web_search:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]
        try:
            r = requests.post(ANTHROPIC_URL, json=body, timeout=timeout, headers={
                "x-api-key": settings.get_secret(conn, "anthropic_api_key"),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            })
        except requests.RequestException as e:
            raise AIError(f"Couldn't reach Claude: {e}")
        if r.status_code >= 400:
            raise AIError(_api_error("Claude", r))
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text, data.get("model") or config.ANTHROPIC_MODEL

    # Gemini
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if web_search:
        body["tools"] = [{"google_search": {}}]
    try:
        r = requests.post(GEMINI_URL.format(model=config.GEMINI_MODEL), json=body, timeout=timeout,
                          headers={"x-goog-api-key": settings.get_secret(conn, "gemini_api_key"),
                                   "content-type": "application/json"})
    except requests.RequestException as e:
        raise AIError(f"Couldn't reach Gemini: {e}")
    if r.status_code >= 400:
        raise AIError(_api_error("Gemini", r))
    data = r.json()
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    text = "".join(p.get("text", "") for p in parts)
    # Grounded answers keep their sources in metadata; append them so URL
    # extraction (board finder) can see them.
    gm = ((data.get("candidates") or [{}])[0].get("groundingMetadata") or {})
    for ch in gm.get("groundingChunks") or []:
        uri = (ch.get("web") or {}).get("uri")
        if uri:
            text += f"\n{uri}"
    return text, config.GEMINI_MODEL


def _api_error(name: str, r: requests.Response) -> str:
    try:
        msg = r.json().get("error", {})
        msg = msg.get("message") if isinstance(msg, dict) else str(msg)
    except Exception:
        msg = r.text[:200]
    if r.status_code in (401, 403):
        return f"{name} rejected the API key ({r.status_code}). Check it in Resume → AI settings."
    if r.status_code == 429:
        return f"{name} rate limit / quota reached. Try again in a minute."
    return f"{name} error {r.status_code}: {msg}"


def extract_json(text: str) -> Any:
    """Pull the first JSON object out of a model reply (fenced or bare)."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else None
    if raw is None:
        start = text.find("{")
        end = text.rfind("}")
        raw = text[start:end + 1] if start >= 0 and end > start else ""
    try:
        return json.loads(raw)
    except Exception:
        raise AIError("The AI reply wasn't valid JSON. Try again.")


# --- Resume tailoring ----------------------------------------------------------

TAILOR_SYSTEM = """You are an expert technical recruiter and resume writer helping a job seeker tailor their resume to one specific job posting.

HARD RULES — never break these:
- Use ONLY facts present in the candidate's resume. Never invent or inflate employers, job titles, dates, degrees, certifications, metrics, numbers, team sizes, or skills.
- You MAY: reorder sections and bullets, rephrase bullets to use the posting's terminology when it is truthful, emphasize relevant experience, drop or shorten less relevant items, and rewrite the summary and skills grouping.
- If the posting asks for something the candidate doesn't have, do NOT add it — list it under "gaps" instead.
- Keep the candidate's name and contact details exactly as given.
- Target one page: roughly 450-650 words of resume content.
- Write plainly: no buzzword stuffing, no first-person pronouns in bullets, no em dashes.
- Respond with ONE JSON object and nothing else."""

TAILOR_SCHEMA = """{
  "fit_summary": "2-3 honest sentences on how well the candidate fits this role",
  "match_score": 0-100,
  "strengths": ["specific reasons they fit"],
  "gaps": ["requirements they don't clearly meet (be honest)"],
  "red_flags": ["anything about the POSTING itself that looks like a scam, bait-and-switch, or unrealistic requirements; empty if none"],
  "changes": ["what you changed in the resume and why"],
  "keywords_used": ["posting keywords you worked in truthfully"],
  "resume": {
    "name": "", "headline": "", "contact": ["line 1", "line 2"],
    "summary": "",
    "skills": [{"category": "", "items": [""]}],
    "experience": [{"company": "", "location": "", "dates": "", "title": "",
                    "bullets": [""],
                    "projects": [{"name": "", "tech": "", "bullets": [""]}]}],
    "projects": [{"name": "", "tech": "", "bullets": [""]}],
    "education": [""]
  },
  "cover_letter": "only if requested, otherwise empty string"
}"""


def tailor(conn: sqlite3.Connection, resume_text: str, job: dict,
           cover_letter: bool = False) -> tuple[dict, str]:
    desc = (job.get("description") or "").strip()
    if len(desc) < 200:
        raise AIError("This posting has no usable description to tailor against. "
                      "Open the posting, copy its text, and paste it into the "
                      "'Job description' box in this panel — then try again.")
    user = f"""JOB POSTING
Title: {job.get('title')}
Company: {job.get('company_name')}
Location: {job.get('location_raw') or 'n/a'}
Salary: {job.get('salary_text') or 'n/a'}

{desc[:12000]}

CANDIDATE RESUME (source of truth)
{resume_text[:12000]}

TASK
Tailor the resume to this posting following the rules. {"Also write a concise, specific cover letter (250-350 words, no clichés, no invented facts) in cover_letter." if cover_letter else "Leave cover_letter as an empty string."}
Return JSON exactly in this shape:
{TAILOR_SCHEMA}"""
    text, model = complete(conn, TAILOR_SYSTEM, user, max_tokens=6000)
    data = extract_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("resume"), dict):
        raise AIError("The AI reply was missing the resume. Try again.")
    return data, model


# --- Board finding -------------------------------------------------------------

def find_board_urls(conn: sqlite3.Connection, company: str, near: Optional[str]) -> tuple[list[str], str]:
    system = ("You find official job boards for companies. Use web search. "
              "Answer with the URLs only, most likely first, one per line.")
    user = (f"Find the official careers / job openings page for the company \"{company}\""
            + (f" (it has jobs near {near})" if near else "") + ". "
            "Strongly prefer the direct applicant-tracking-system board where the job list lives "
            "(e.g. boards.greenhouse.io/..., jobs.lever.co/..., *.myworkdayjobs.com/..., "
            "jobs.ashbyhq.com/..., *.bamboohr.com/careers, apply.workable.com/..., *.icims.com, "
            "recruiting.paylocity.com/...). Also include the company's own careers page URL. "
            "Do not include LinkedIn, Indeed, Glassdoor or other job aggregators.")
    text, model = complete(conn, system, user, max_tokens=800, web_search=True, timeout=90)
    urls = re.findall(r"https?://[^\s\)\]\"'<>,]+", text)
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".")
        if u not in seen and not re.search(r"indeed\.|linkedin\.|glassdoor\.|ziprecruiter\.", u):
            seen.add(u)
            out.append(u)
    return out[:12], model
