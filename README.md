# jobhunt

A personal job board. Searches every company hiring near your ZIP code —
your tracked companies' boards directly, plus Indeed / ZipRecruiter / Google
Jobs for everyone else — and shows you only what's new since you last looked.

## What's new in v2

- **Everything is configured in the UI now.** Click ⚙ Settings to set your
  ZIP code, search radius, keywords to look for, and keywords to exclude.
  No environment variables, no restarts.
- **Finds any company, not just your list.** Indeed, ZipRecruiter, and Google
  Jobs are searched around your ZIP (via [JobSpy](https://github.com/speedyapply/JobSpy)),
  so a bakery two towns over that you've never heard of shows up too. Tracked
  companies are still fetched straight from their own boards — that feed is
  faster and more accurate, so keep your favorites on the list.
- **Auto-discovery grows your list for you.** When an aggregate site finds an
  in-range posting from a company you don't track, the refresh tries to detect
  that company's job board (Greenhouse, Lever, Ashby, SmartRecruiters,
  Workday) and, if a live board confirms, adds it to your tracked list
  automatically. From then on that company is polled directly — near
  real-time instead of waiting for Indeed to index it. Confirm-or-nothing, at
  most 5 probes per refresh (configurable), misses retried monthly. Toggle it
  in ⚙ Settings.
- **Much faster.** Company boards are fetched in parallel instead of one at a
  time, and locations are geocoded from an offline index of every US ZIP
  (~1 ms) instead of a rate-limited web service (~1.1 s each). A first refresh
  that used to take several minutes now takes seconds.
- **Instant filter changes.** Keywords and radius are applied when you view
  the board, not when jobs are fetched. Add an exclude keyword and the list
  updates immediately. Widen your radius and jobs that were previously "too
  far" appear without refetching. Change your ZIP and every stored job's
  distance is re-measured on the spot.
- **Live refresh progress.** The refresh runs in the background with a
  progress bar, so the button doesn't freeze for a minute.
- **Companies manager in the UI.** Add or remove tracked companies without
  touching the CLI (the CLI still works).

## Try it on your laptop

```bash
pip install -r requirements.txt
pip install python-jobspy --no-deps

uvicorn app.server:app --port 8081
# open http://127.0.0.1:8081
# click ⚙ Settings, set your ZIP + radius + keywords, hit Refresh
```

> **Why `--no-deps`?** python-jobspy pins `numpy==1.26.3`, which has no
> prebuilt wheels for Python 3.13+ and fails to compile on machines without
> a C toolchain (typical on Windows). jobspy runs fine on numpy 2.x, so
> `requirements.txt` installs compatible versions of all of jobspy's
> dependencies, and jobspy itself is installed without pulling its pins.
> pip may print a warning about the numpy pin — it's safe to ignore.

Optional CLI equivalents still exist:

```bash
python -m app.cli init
python -m app.cli add "Lucid Software" --url https://lucid.co/careers
python -m app.cli add-batch companies.txt
python -m app.cli refresh
python -m app.cli jobs --new
```

## Settings (all in the UI)

| Setting | What it does |
|---|---|
| ZIP code | Center of your search. Validated against a real US ZIP list; shows the city name so you know you typed it right. Changing it re-measures every stored job instantly. |
| Radius | Miles from your ZIP. Applied at view time — widening it reveals already-fetched jobs immediately. |
| Keywords to look for | Titles must contain at least one (empty = everything). Also used as the search terms sent to Indeed & friends. |
| Keywords to exclude | Any title containing one of these is hidden. Instant. |
| Include remote | Whether remote postings show up. |
| Include unpinpointed | Postings whose location text couldn't be resolved ("5 Locations") are kept and flagged rather than silently lost. |
| Where to search | Company boards / Indeed / ZipRecruiter / Google Jobs, each toggleable. |
| Results per site, max age | Caps for the aggregate searches. |

Environment variables from v1 (`HOME_ZIP`, `RADIUS_MILES`, `TITLE_INCLUDE`, …)
still seed the *defaults* on a fresh database, but the UI owns them after that.

## How the search works

1. **Tracked companies** (Greenhouse, Lever, Ashby, SmartRecruiters, Workday)
   are fetched in parallel straight from their public board APIs.
2. **Aggregate sites** are searched around your ZIP within your radius, one
   search per include-keyword (or one broad search if you have none).
   In-range companies they surface that you don't track yet are candidates for
   **auto-discovery**: their board is probed, and confirmed boards join your
   tracked list and are fetched in the same refresh.
3. Every posting's location is resolved offline against a bundled index of
   ~43k US ZIP codes and city centroids. Only genuinely weird strings fall
   back to Nominatim, rate-limited and budgeted (40/run) so they can never
   stall a refresh.
4. Postings are deduplicated by company + normalized title + canonical
   location, so the same job found on a company's own board and on Indeed is
   one row (the direct board's URL wins).
5. Everything US-based is stored — including jobs outside your current radius —
   with a computed distance. Filtering happens when you look at the board,
   which is what makes settings changes instant. Foreign postings are dropped.

## How "new" works

`seen_at` is per-job and only set when you actually open or dismiss it. A
refresh at 3am doesn't bury anything. Jobs that stop appearing get `gone_at`
set rather than deleted, so your applied history survives; gone-marking is
skipped for any refresh where half the sources errored.

## Keyboard

`j`/`k` move · `x` select · `o` open · `s` star · `a` applied · `e` seen ·
`h` hide · `O` open all selected

The point is `x x x O`: select the new ones, open them all in tabs, and
they're marked seen on the way out.

## Run on the Windows 11 server

```powershell
docker compose up -d --build
```

Then point your reverse proxy at `http://<server-ip>:8081`. The `scheduler`
container refreshes every 2 hours by default; set `REFRESH_EVERY_MINUTES` in a
`.env` file to tune it (60 is a sensible floor — the aggregate sites throttle
aggressive polling). Fresh postings can only appear as fast as you poll. Data
lives in the `jobhunt-data` volume, so rebuilds keep your history, seen flags,
applied marks, and settings.

Back it up with:

```powershell
docker run --rm -v jobhunt-data:/data -v ${PWD}:/backup alpine tar czf /backup/jobhunt-backup.tar.gz /data
```

## Notes on the aggregate sites

Indeed / ZipRecruiter / Google don't have public APIs; JobSpy scrapes their
public search pages. That works well for personal daily use, but a site may
occasionally throttle or block a request — those show up as source errors in
the status line, and the refresh carries on with everything else. LinkedIn and
Glassdoor are intentionally not enabled: they block scrapers aggressively and
mostly duplicate Indeed's listings.

## Tests

```bash
python test_v2.py        # v2: offline geo, settings, pipeline, query filters
python test_core.py      # hashing, dedupe, state preservation
python test_fetchers.py  # ATS parsing against fixtures
```
# jobhunt
