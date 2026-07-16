"""Re-run geo classification over jobs already in the DB.

Use after changing RADIUS_MILES or the geo logic. Only touches geo columns;
seen/applied/starred/notes are never modified.

    python3 tools_backfill_geo.py          # apply
    python3 tools_backfill_geo.py --dry    # preview only
"""
import sys
from app import db, geo, config

dry = "--dry" in sys.argv
conn = db.connect()
db.init_db(conn)

rows = conn.execute("SELECT id, location_raw FROM jobs").fetchall()
counts = {}
updates = []
for r in rows:
    res = geo.resolve_location(conn, r["location_raw"])
    counts[res["status"]] = counts.get(res["status"], 0) + 1
    updates.append((res["distance_miles"], res["lat"], res["lon"],
                    res["status"], res["country"],
                    1 if res["status"] == "remote" else 0, r["id"]))

if not dry:
    conn.executemany(
        """UPDATE jobs SET distance_miles=?, lat=?, lon=?, geo_status=?,
                           country=?, remote=? WHERE id=?""", updates)
    conn.commit()

print(f"{'DRY RUN - ' if dry else ''}{len(rows)} jobs classified:")
for k in sorted(counts):
    print(f"  {k:<10} {counts[k]}")
keep = counts.get("in_range", 0) + counts.get("remote", 0) + (
    counts.get("unknown", 0) if config.KEEP_UNKNOWN_LOCATIONS else 0)
print(f"\nvisible on board: {keep}   hidden: {len(rows) - keep}")
