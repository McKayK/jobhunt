"""Resume <-> job matching, locally (no AI, no cost, instant).

Finds known skills/tools in your default resume and in each job's title +
description, then scores how much of what the job asks for you already list.
It's deliberately simple and explainable: the UI shows exactly which skills
matched and which are missing, so you can judge for yourself (and the AI
tailoring step can use the same list).

Score = 75% skill coverage + 25% role fit (does the title look like the kind
of role your resume targets). Jobs that mention fewer than 3 known skills get
a score from role fit only, capped at 60, so a terse posting can't look like a
perfect match.
"""
from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from typing import Optional

# canonical skill -> aliases (regex fragments, matched case-insensitively on
# word-ish boundaries). Keep canonical names short; they're shown in the UI.
SKILLS: dict[str, list[str]] = {
    # Languages
    "JavaScript": [r"javascript", r"\bjs\b", r"es6"],
    "TypeScript": [r"typescript", r"\bts\b"],
    "Python": [r"python"],
    "Java": [r"\bjava\b(?!\s*script)"],
    "C#": [r"c#", r"c sharp"],
    "C++": [r"c\+\+"],
    "Go": [r"\bgolang\b", r"\bgo\b(?= (developer|engineer|programming|language))"],
    "Rust": [r"\brust\b"],
    "Ruby": [r"\bruby\b"],
    "PHP": [r"\bphp\b"],
    "Swift": [r"swiftui", r"\b(ios|objective-c|xcode)\b.{0,40}\bswift\b", r"\bswift\b.{0,40}\b(ios|objective-c|xcode)\b"],
    "Kotlin": [r"kotlin"],
    "SQL": [r"\bsql\b"],
    "HTML": [r"\bhtml5?\b"],
    "CSS": [r"\bcss3?\b", r"\bsass\b", r"\bscss\b"],
    "Bash/Shell": [r"\bbash\b", r"shell script", r"powershell"],
    # Front end
    "React": [r"\breact(\.js|js)?\b(?! native)"],
    "React Native": [r"react native"],
    "Next.js": [r"next\.?js"],
    "Vue": [r"\bvue(\.js|js)?\b"],
    "Angular": [r"\bangular(js)?\b"],
    "Svelte": [r"svelte"],
    "Tailwind": [r"tailwind"],
    "Redux": [r"\bredux\b"],
    "Expo": [r"\bexpo\b"],
    "Vite": [r"\bvite\b"],
    "Three.js": [r"three\.?js"],
    "jQuery": [r"jquery"],
    # Back end
    "Node.js": [r"node(\.js|js)?\b"],
    "Express": [r"express\.?js", r"node(\.js)?\s*(/|and|&|,)\s*express\b", r"\bexpress\s*(/|and|&|,)\s*node"],
    "FastAPI": [r"fastapi"],
    "Django": [r"django"],
    "Flask": [r"\bflask\b"],
    "Spring": [r"spring boot", r"spring framework", r"\bspring (mvc|cloud|security|data)\b"],
    ".NET": [r"\.net\b", r"asp\.net", r"dotnet"],
    "Rails": [r"\brails\b"],
    "GraphQL": [r"graphql"],
    "REST APIs": [r"\brest(ful)?\b", r"\bapis?\b"],
    "Socket.io": [r"socket\.?io", r"websockets?"],
    "Microservices": [r"micro-?services"],
    # Data
    "PostgreSQL": [r"postgres(ql)?"],
    "MySQL": [r"mysql"],
    "SQLite": [r"sqlite"],
    "MongoDB": [r"mongo(db)?"],
    "Redis": [r"\bredis\b"],
    "Firebase": [r"firebase", r"firestore"],
    "DynamoDB": [r"dynamodb"],
    "Elasticsearch": [r"elastic ?search"],
    # Cloud / DevOps
    "AWS": [r"\baws\b", r"amazon web services", r"\blambda\b", r"\bs3\b", r"\bec2\b"],
    "Azure": [r"\bazure\b"],
    "GCP": [r"\bgcp\b", r"google cloud"],
    "Docker": [r"docker", r"container(s|ization)?\b"],
    "Kubernetes": [r"kubernetes", r"\bk8s\b"],
    "Terraform": [r"terraform"],
    "CI/CD": [r"ci/cd", r"\bci\b", r"continuous (integration|delivery|deployment)", r"github actions", r"jenkins"],
    "Linux": [r"\blinux\b", r"\bubuntu\b", r"\bunix\b"],
    "Nginx": [r"nginx"],
    "Git": [r"\bgit\b", r"github", r"gitlab"],
    # Testing / QA
    "Jest": [r"\bjest\b"],
    "Cypress": [r"cypress"],
    "Playwright": [r"playwright"],
    "Selenium": [r"selenium"],
    "pytest": [r"pytest"],
    "Testing/QA": [r"\bqa\b", r"quality assurance", r"unit test", r"test automation", r"automated test"],
    # IT / support
    "IT Support": [r"help ?desk", r"service ?desk", r"desktop support", r"technical support", r"it support"],
    "Troubleshooting": [r"troubleshoot", r"troubleshot"],
    "Networking": [r"networking", r"tcp/ip", r"\bdns\b", r"\bdhcp\b", r"\bvpn\b", r"\blan\b", r"firewall"],
    "VoIP": [r"\bvoip\b"],
    "Windows": [r"\bwindows\b"],
    "macOS": [r"\bmac ?os\b", r"\bmacos\b"],
    "Active Directory": [r"active directory", r"\bentra\b", r"azure ad"],
    "Microsoft 365": [r"office 365", r"microsoft 365", r"\bo365\b", r"\bm365\b"],
    "Hardware": [r"hardware", r"printers?", r"peripherals"],
    "Ticketing": [r"servicenow", r"zendesk", r"ticketing", r"\bjira\b"],
    # AI
    "AI/LLMs": [r"\bllms?\b", r"large language model", r"generative ai", r"\bgenai\b", r"openai", r"gemini", r"claude", r"\bai\b"],
    "Machine Learning": [r"machine learning", r"\bml\b"],
    # Practices / tools
    "Agile/Scrum": [r"\bagile\b", r"\bscrum\b", r"kanban"],
    "Figma": [r"figma"],
    "Postman": [r"postman"],
    "Mobile": [r"\bmobile\b", r"\bios\b", r"\bandroid\b"],
    "E-commerce": [r"e-?commerce", r"shopify"],
    "WordPress": [r"wordpress"],
    "ffmpeg": [r"ffmpeg"],
}

_COMPILED = {k: re.compile("|".join(f"(?:{a})" for a in v), re.I) for k, v in SKILLS.items()}

ROLE_WORDS = re.compile(
    r"\b(developer|engineer|programmer|full[\s-]?stack|front[\s-]?end|back[\s-]?end|software|"
    r"web|mobile|devops|qa|test|it support|help ?desk|technician|systems? admin|support engineer|"
    r"application|integration)\b", re.I)


def skills_in(text: Optional[str]) -> set[str]:
    if not text:
        return set()
    return {k for k, p in _COMPILED.items() if p.search(text)}


def years_required(text: Optional[str]) -> Optional[int]:
    """'5+ years of experience' -> 5 (the largest plausible number found)."""
    if not text:
        return None
    nums = [int(m.group(1)) for m in re.finditer(
        r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)(?:'|’)?\s*(?:of\s+)?(?:professional\s+|relevant\s+|hands-on\s+)?(?:experience|exp\b)",
        text, re.I)]
    nums = [n for n in nums if 0 < n <= 20]
    return max(nums) if nums else None


def score(resume_skills: set[str], title: str, description: Optional[str]) -> tuple[Optional[int], dict]:
    job_sk = skills_in(f"{title}\n{description or ''}")
    role_fit = 1.0 if ROLE_WORDS.search(title or "") else 0.0
    matched = sorted(job_sk & resume_skills)
    missing = sorted(job_sk - resume_skills)
    detail = {"matched": matched, "missing": missing,
              "years": years_required(description)}
    if not resume_skills:
        return None, detail
    if len(job_sk) < 3:
        return int(round(60 * role_fit)) if description else None, detail
    coverage = len(matched) / len(job_sk)
    return int(round(100 * (0.75 * coverage + 0.25 * role_fit))), detail


@lru_cache(maxsize=4)
def _resume_skills_cached(text: str) -> frozenset:
    return frozenset(skills_in(text))


def default_resume_text(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT text FROM resumes ORDER BY is_default DESC, updated_at DESC LIMIT 1").fetchone()
    return row["text"] if row else None


def rescore(conn: sqlite3.Connection, only_missing: bool = False) -> int:
    """Recompute match scores for live jobs against the default resume."""
    text = default_resume_text(conn)
    if not text:
        conn.execute("UPDATE jobs SET match_score = NULL, match_detail = NULL WHERE match_score IS NOT NULL")
        conn.commit()
        return 0
    rs = set(_resume_skills_cached(text))
    where = "gone_at IS NULL" + (" AND match_detail IS NULL" if only_missing else "")
    rows = conn.execute(f"SELECT id, title, description FROM jobs WHERE {where}").fetchall()
    for r in rows:
        s, d = score(rs, r["title"], r["description"])
        conn.execute("UPDATE jobs SET match_score=?, match_detail=? WHERE id=?",
                     (s, json.dumps(d), r["id"]))
    conn.commit()
    return len(rows)
