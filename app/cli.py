"""Command-line interface.

    python -m app.cli init
    python -m app.cli add "Company Name" --url https://company.com/careers
    python -m app.cli add-batch companies.txt
    python -m app.cli detect "Company Name" --url ...
    python -m app.cli refresh
    python -m app.cli jobs --new
    python -m app.cli companies
"""
from __future__ import annotations

import argparse
import sys

from . import config, db, detect as detect_mod, refresh as refresh_mod
from .fetchers import ats as ats_fetchers

# Derived from the fetcher registry rather than hardcoded: a hardcoded tuple
# silently skipped every Workday company after its fetcher landed.
SUPPORTED = tuple(ats_fetchers.FETCHERS.keys())


def cmd_init(args):
    conn = db.connect()
    db.init_db(conn)
    print(f"Initialized {config.DB_PATH}")
    conn.close()


def cmd_detect(args):
    d = detect_mod.detect(args.name, args.url, probe=not args.no_probe)
    print(f"  company    : {args.name}")
    print(f"  ats        : {d.ats}")
    print(f"  slug       : {d.slug}")
    if d.workday_host:
        print(f"  wd host    : {d.workday_host}{d.workday_path or ''}")
    print(f"  confidence : {d.confidence}")
    print(f"  note       : {d.note}")
    print(f"  supported  : {'yes' if d.ats in SUPPORTED else 'NO - needs manual handling'}")


def _add_one(conn, name, url, quiet=False):
    d = detect_mod.detect(name, url, probe=True)
    if d.ats not in SUPPORTED or not d.slug:
        if not quiet:
            print(f"  [SKIP] {name:<30} {d.ats:<12} {d.note}")
        return False
    # Workday is identified by tenant host + site path, not slug alone. Storing
    # it without them yields a row that detects fine and then fails every fetch.
    if d.ats == "workday" and not (d.workday_host and d.workday_path):
        if not quiet:
            if d.workday_host:
                # We know the tenant but not which site to ask for. Guessing is
                # how fabricated slugs got in last time, so surface it instead.
                print(f"  [SKIP] {name:<30} workday      "
                      f"found tenant {d.workday_host} but no site path. Open "
                      f"https://{d.workday_host}/ , click into the job list, and "
                      f"put the full URL in companies.txt")
            else:
                print(f"  [SKIP] {name:<30} workday      "
                      f"need full tenant URL (host + site path)")
        return False
    cid = db.add_company(conn, name, url, d.ats, d.slug,
                         d.workday_host, d.workday_path)
    if not quiet:
        print(f"  [OK]   {name:<30} {d.ats}/{d.slug}  ({d.confidence})")
    return True


def cmd_add(args):
    conn = db.connect()
    db.init_db(conn)
    _add_one(conn, args.name, args.url)
    conn.close()


def cmd_add_batch(args):
    """Each line: Company Name | https://careers-url  (url optional)"""
    conn = db.connect()
    db.init_db(conn)
    ok = fail = 0
    for line in open(args.file):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        url = parts[1] if len(parts) > 1 and parts[1] else None
        if _add_one(conn, name, url):
            ok += 1
        else:
            fail += 1
    print(f"\nAdded {ok}, skipped {fail}")
    conn.close()


def cmd_refresh(args):
    conn = db.connect()
    db.init_db(conn)
    from . import settings as settings_mod
    cfg = settings_mod.get_all(conn)
    print(f"Refreshing (ZIP {cfg['home_zip']}, radius {cfg['radius_miles']}mi)...")
    t = refresh_mod.refresh_all(conn, verbose=True)
    print(f"\n{t['companies']} companies | {t['found']} found | "
          f"{t['stored']} stored | {t['new']} NEW")
    if t["errors"]:
        print(f"\n{len(t['errors'])} error(s):")
        for e in t["errors"]:
            print(f"  {e}")
    conn.close()


def cmd_jobs(args):
    conn = db.connect()
    db.init_db(conn)
    where = "seen_at IS NULL AND hidden = 0 AND gone_at IS NULL" if args.new else "hidden = 0"
    rows = conn.execute(
        f"""SELECT company_name, title, location_raw, distance_miles, remote, posted_at, url
              FROM jobs WHERE {where}
             ORDER BY COALESCE(posted_at, first_seen_at) DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No jobs. Run: python -m app.cli refresh")
    for r in rows:
        dist = ("remote" if r["remote"]
                else f"{r['distance_miles']:.0f}mi" if r["distance_miles"] is not None
                else "?")
        print(f"  {r['company_name'][:18]:<20} {r['title'][:44]:<46} "
              f"{dist:<8} {r['posted_at'] or ''}")
        print(f"      {r['url']}")
    print(f"\n{len(rows)} job(s)")
    conn.close()


def cmd_companies(args):
    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute("""
        SELECT c.*, COUNT(j.id) n FROM companies c
        LEFT JOIN jobs j ON j.company_id = c.id AND j.gone_at IS NULL
        WHERE c.active = 1 GROUP BY c.id ORDER BY c.name
    """).fetchall()
    for r in rows:
        err = f"  ERROR: {r['last_error'][:60]}" if r["last_error"] else ""
        print(f"  [{r['id']:>3}] {r['name'][:24]:<26} {r['ats']}/{r['slug']:<22} "
              f"{r['n']:>3} jobs{err}")
    print(f"\n{len(rows)} active companies")
    conn.close()


def cmd_verify(args):
    """Re-probe every stored company and report which slugs are fabricated.

    Needed because an earlier probe accepted HTTP 200 as proof, and some ATS
    APIs return 200 with an empty body for slugs that don't exist.
    """
    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute(
        "SELECT * FROM companies WHERE active = 1 AND slug IS NOT NULL ORDER BY name"
    ).fetchall()

    good, bad = [], []
    print(f"Verifying {len(rows)} companies against their live boards...\n")
    for r in rows:
        ok = detect_mod.probe_row(r)
        if ok is None:
            continue
        # A board can legitimately have zero openings right now, so re-check
        # against the fetcher before condemning it.
        if not ok:
            try:
                jobs = ats_fetchers.fetch_company(r["name"], r["ats"], r["slug"], r)
                ok = len(jobs) > 0
            except Exception:
                ok = False
        (good if ok else bad).append(r)
        print(f"  [{'OK  ' if ok else 'DEAD'}] {r['name'][:30]:<32} {r['ats']}/{r['slug']}")

    print(f"\n{len(good)} verified, {len(bad)} unverifiable")
    if bad:
        print("\nUnverifiable (no live board found — likely a guessed slug):")
        for r in bad:
            print(f"  [{r['id']:>3}] {r['name'][:30]:<32} {r['ats']}/{r['slug']}")
        print(f"\nRemove them all with:  python -m app.cli prune --yes")
    conn.close()


def cmd_prune(args):
    """Deactivate companies whose boards can't be verified."""
    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute(
        "SELECT * FROM companies WHERE active = 1 AND slug IS NOT NULL"
    ).fetchall()

    bad = []
    for r in rows:
        probed = detect_mod.probe_row(r)
        if probed is None or probed:
            continue
        try:
            if ats_fetchers.fetch_company(r["name"], r["ats"], r["slug"], r):
                continue
        except Exception:
            pass
        bad.append(r)

    if not bad:
        print("Nothing to prune. Every company has a live board.")
        conn.close()
        return

    print(f"{len(bad)} companies have no reachable board:")
    for r in bad:
        print(f"  {r['name'][:30]:<32} {r['ats']}/{r['slug']}")

    if not args.yes:
        print("\nRe-run with --yes to deactivate these.")
        conn.close()
        return

    for r in bad:
        conn.execute("UPDATE companies SET active = 0 WHERE id = ?", (r["id"],))
    conn.commit()
    print(f"\nDeactivated {len(bad)}. Their job history is kept.")
    conn.close()


def cmd_dupes(args):
    """Find companies that are probably the same employer under two slugs."""
    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute("SELECT * FROM companies WHERE active = 1 ORDER BY name").fetchall()

    import re as _re

    def key(n):
        n = n.lower()
        n = _re.sub(r"\(.*?\)", " ", n)                    # "(formerly X)"
        n = _re.sub(r"\b(formerly|now|inc|llc|ltd|corp|corporation|co|company|"
                    r"technologies|technology|labs|software|systems|group|"
                    r"holdings|grills|wireless|financial|fund)\b", " ", n)
        return _re.sub(r"[^a-z0-9]", "", n)

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(key(r["name"]), []).append(r)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupes:
        print("No likely duplicates found.")
    for k, v in dupes.items():
        print(f"\nLikely the same employer:")
        for r in v:
            n = conn.execute(
                "SELECT COUNT(*) c FROM jobs WHERE company_id = ? AND gone_at IS NULL",
                (r["id"],),
            ).fetchone()["c"]
            print(f"  [{r['id']:>3}] {r['name'][:32]:<34} {r['ats']}/{r['slug']:<28} {n} jobs")
        print(f"  -> keep the one with jobs; drop others with: "
              f"python -m app.cli drop <id>")
    conn.close()


def cmd_drop(args):
    conn = db.connect()
    db.init_db(conn)
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"No company with id {args.id}")
        conn.close()
        return
    conn.execute("UPDATE companies SET active = 0 WHERE id = ?", (args.id,))
    conn.commit()
    print(f"Deactivated [{args.id}] {row['name']} ({row['ats']}/{row['slug']})")
    conn.close()


def cmd_recheck(args):
    """Re-run detection on companies whose board can't be reached.

    Their stored slug was likely guessed by the old, broken probe. This tries
    the fixed pipeline (directory lookup first) and repairs anything it can.
    """
    conn = db.connect()
    db.init_db(conn)
    rows = conn.execute(
        "SELECT * FROM companies WHERE active = 1 AND slug IS NOT NULL ORDER BY name"
    ).fetchall()

    dead = []
    print(f"Finding unreachable companies among {len(rows)}...")
    for r in rows:
        if detect_mod.probe_row(r):
            continue
        try:
            if ats_fetchers.fetch_company(r["name"], r["ats"], r["slug"], r):
                continue
        except Exception:
            pass
        dead.append(r)

    print(f"{len(dead)} unreachable. Re-detecting with the directory lookup...\n")

    fixed = still_dead = 0
    for r in dead:
        d = detect_mod.detect(r["name"], r["careers_url"], probe=True)
        if d.ats in SUPPORTED and d.slug:
            same = (d.ats == r["ats"] and d.slug == r["slug"])
            if same:
                print(f"  [DEAD] {r['name'][:30]:<32} still {d.ats}/{d.slug}")
                still_dead += 1
                continue
            conn.execute("UPDATE companies SET active = 0 WHERE id = ?", (r["id"],))
            conn.commit()
            db.add_company(conn, r["name"], r["careers_url"], d.ats, d.slug)
            print(f"  [FIX ] {r['name'][:30]:<32} {r['ats']}/{r['slug']}  ->  {d.ats}/{d.slug}")
            fixed += 1
        else:
            print(f"  [DEAD] {r['name'][:30]:<32} {d.ats}  {d.note[:44]}")
            still_dead += 1

    print(f"\n{fixed} repaired, {still_dead} still unreachable")
    if still_dead:
        print("Drop the rest with:  python -m app.cli prune --yes")
    conn.close()


def main():
    p = argparse.ArgumentParser(prog="jobhunt")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)

    a = sub.add_parser("add"); a.add_argument("name"); a.add_argument("--url")
    a.set_defaults(fn=cmd_add)

    a = sub.add_parser("add-batch"); a.add_argument("file"); a.set_defaults(fn=cmd_add_batch)

    a = sub.add_parser("detect"); a.add_argument("name"); a.add_argument("--url")
    a.add_argument("--no-probe", action="store_true"); a.set_defaults(fn=cmd_detect)

    sub.add_parser("refresh").set_defaults(fn=cmd_refresh)

    a = sub.add_parser("jobs"); a.add_argument("--new", action="store_true")
    a.add_argument("--limit", type=int, default=50); a.set_defaults(fn=cmd_jobs)

    sub.add_parser("companies").set_defaults(fn=cmd_companies)

    sub.add_parser("verify").set_defaults(fn=cmd_verify)

    sub.add_parser("recheck").set_defaults(fn=cmd_recheck)

    a = sub.add_parser("prune"); a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_prune)

    sub.add_parser("dupes").set_defaults(fn=cmd_dupes)

    a = sub.add_parser("drop"); a.add_argument("id", type=int); a.set_defaults(fn=cmd_drop)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()