# jobhunt — personal job board

Pulls jobs from companies' own hiring systems and from the big job sites,
filters them to your area (plus remote jobs open to anyone in the US),
flags scams, scores every job against your resume, and tracks what you've
applied to. Built for daily use by you and your family.

## What's new in v3

**More jobs, more of them from company sites**
- **Apply-link discovery.** Indeed now hands back each employer's own apply
  link (JobSpy 1.4). When that link points at a company's job board —
  Greenhouse, Lever, Workday, Ashby, BambooHR, Workable, … — the company is
  tracked automatically and its whole board is pulled straight from the
  source from then on. This is the only automatic way to find **Workday**
  companies (their addresses can't be guessed), so companies like Domo now
  get picked up without you doing anything.
- **9 hiring systems** read directly: Greenhouse, Lever, Ashby, SmartRecruiters,
  Workday (both URL styles), plus new **Workable, BambooHR, Breezy, Recruitee**.
  BambooHR is very common among Utah small/mid-size employers.
- **Ashby fixed.** The old fetcher had silently started returning nothing
  (Weave, Instructure and Jane showed 0 jobs). It now uses Ashby's public
  posting API, with full descriptions and street addresses.
- **LinkedIn and Glassdoor** available as optional sources.
- **Job titles link to the company's own application page** when we know it,
  with a small "via Indeed" link beside it.
- **Starter company list** (36 verified Utah County employers) loads
  automatically into an empty database, or any time from Companies → Import.

**Remote jobs anywhere in the US**
- Your keywords are also searched US-wide, remote-only, on the job sites,
  plus four remote-only boards (Himalayas, Jobicy, Remotive, Remote OK).
- Remote jobs limited to other countries are dropped; ones limited to other
  US states ("Remote – California residents") are kept out of view.

**Scam & spam filter**
- Every job gets a risk score with plain-English reasons: chat-app interviews,
  check/equipment schemes, reshipping, task scams, upfront fees, SSN/bank
  requests, personal email contacts, too-good pay, vague postings, employers
  with no web presence, the same posting copied to many cities.
- Employers' own anti-scam disclaimers ("we will never ask for your SSN")
  don't count against them, and postings on a real company board get a big
  trust bonus.
- Likely scams move to a **Flagged** tab instead of disappearing. Staffing
  agencies are tagged and can be hidden. Any company can be blocked.

**Tracking what you applied to**
- Opening a job no longer makes it vanish. It stays in the list (dimmed),
  and when you come back to the tab, a small card asks **"Did you apply?"**
  — Yes / Save for later / Not for me / Not yet.
- **Recently viewed** tab: every job you opened in the last 30 days.
- **Applications** tab with statuses (applied → interviewing → offer /
  rejected / withdrawn) and notes per job.
- Click any job for a detail panel: description, legitimacy check, resume
  match, status, notes, links.

**Your resume**
- Upload your resume (PDF/Word/text). Every job gets a **match %** with the
  skills you have and the ones it asks for — free, instant, no AI.
- **Tailor resume** (optional AI): rewrites your resume for one posting using
  only facts already in it, explains what changed, lists honest gaps and any
  red flags in the posting, and downloads as a Word file (plus an optional
  cover letter). Needs a Claude or Gemini API key.

**Fixes**
- Indeed jobs were being expired every refresh (absence from a capped search
  ≠ closed). They now expire after 10 days unseen.
- One company's board erroring no longer expires other companies' jobs.
- Re-adding a company now fixes a wrong Workday address (the Domo bug).
- Keyword filters match whole words: "intern" no longer hides "Internal Tools
  Engineer" or "International".
- Locations like "Lehi", "Salt Lake City (Hybrid)", "India - Pune" are now
  understood offline (~190 jobs were stuck as "unknown").

## Run it on your laptop

```bash
pip install -r requirements.txt
uvicorn app.server:app --port 8081
# open http://127.0.0.1:8081
```

No more separate `pip install python-jobspy --no-deps` step: JobSpy 1.4.0
dropped the old numpy pin and installs normally on Python 3.13.

## Update the server (Windows 11 + Docker)

From `M:\docker\jobhunt` in **PowerShell**:

```powershell
git pull
docker compose up -d --build
```

Your data lives in the `jobhunt_jobhunt-data` volume, so it survives
rebuilds. On first start the app upgrades the database by itself (new
columns, re-classifies locations, scores every job) — takes a few seconds.

Then in the app: **Resume → Add resume** (upload your PDF), and optionally
paste an AI key there.

**One-time git cleanup.** `data/` is now in `.gitignore`, but if a database
was committed earlier, git still tracks it — that's how the server once ended
up with a stale database. On the laptop, run once:

```bash
git rm -r --cached data
git commit -m "Stop tracking the database"
```

This only removes it from git; your local file stays.

## Getting the most out of it

- **Keywords are your search terms.** They drive what Indeed, LinkedIn and
  the remote boards return. A wider set finds more: `developer`, `engineer`,
  `software`, `web`, `full stack`, `frontend`, `backend`, `qa`, and for IT
  roles `it support`, `help desk`, `technician`, `systems administrator`.
- **Exclude keywords are whole words now**, so they're safer to use.
- **Sort by "Best match"** once your resume is in.
- **Find job board** (Companies panel) for any company you're curious about:
  it checks their postings' apply links, their website, likely board
  addresses on 9 systems, a web search, and AI search if you've added a key.
  If they use a system the app can't read (iCIMS, Paylocity, UKG, …), you
  still get the link.

## AI keys (optional)

Only resume tailoring and the last step of "Find job board" use AI.

- **Claude**: create a key at console.anthropic.com. Uses `claude-sonnet-5-5`
  — roughly 4–5¢ per tailored resume.
- **Gemini**: create a key at aistudio.google.com (has a free tier). Uses
  `gemini-3.8-flash`.

Paste a key in **Resume → AI**, or set `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`
in `.env` for docker compose (an environment key always wins). Override
models with `ANTHROPIC_MODEL` / `GEMINI_MODEL`.

The app has no login, so anyone who can reach it can use your AI key. Keep it
on your LAN/VPN or behind your reverse proxy's access list.

## How the search works

1. **Tracked company boards** are fetched in parallel straight from their
   public APIs. A board that answers is complete, so its missing jobs closed.
2. **Job sites** are searched around your ZIP, one search per keyword, and
   (with "anywhere in the US" on) again nationwide for remote-only jobs.
3. **Remote boards** are checked on their own polite schedule (Remotive and
   Remote OK at most every 6 hours, as their terms ask).
4. **Auto-discovery** turns job-site apply links into tracked boards (up to 25
   checks per refresh), then tries name-guessing for up to 10 more nearby
   companies. Only boards that answer with live jobs are added.
5. **Housekeeping**: expire stale job-site postings, run the scam filter,
   score matches against your resume.

Locations resolve offline from a bundled index of ~43k US ZIP codes;
Nominatim is only a capped fallback. Filters apply when you view the board,
so changing ZIP/radius/keywords is instant.

## Keyboard

`j`/`k` move · `Enter` details · `o` open · `a` applied · `s` star ·
`h` hide · `x` select · `O` open selected · `e` toggle seen · `/` search ·
`Esc` close

## Tests

```bash
python test_v3.py      # new sources, discovery, scam filter, matching, tracking, tailoring
python test_v2.py      # geocoding, settings, refresh pipeline, query-time filters
python test_core.py    # identity/dedupe, user-state preservation, expiry
python test_fetchers.py
python test_api.py
python test_discovery.py
```

All tests use a temporary database and mock every network call.
