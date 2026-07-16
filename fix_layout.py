"""Rebuild the app/ and web/ package structure from a flattened download.

Run this from inside the jobBoard folder:

    python fix_layout.py

Safe to run twice; it skips anything already in place.
"""
import os
import shutil
import sys

APP_FILES = ["cli.py", "config.py", "db.py", "detect.py", "geo.py",
             "refresh.py", "server.py", "schema.sql"]
FETCHER_FILES = ["ats.py"]
WEB_FILES = ["index.html"]

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)

moved = skipped = 0


def place(filename, dest_dir):
    global moved, skipped
    dest = os.path.join(dest_dir, filename)
    if os.path.exists(dest):
        print(f"  already there   {dest}")
        skipped += 1
        return
    if not os.path.exists(filename):
        print(f"  MISSING         {filename}  (expected at top level)")
        return
    os.makedirs(dest_dir, exist_ok=True)
    shutil.move(filename, dest)
    print(f"  moved           {filename}  ->  {dest}")
    moved += 1


print("Rebuilding project layout...\n")

for f in APP_FILES:
    place(f, "app")
for f in FETCHER_FILES:
    place(f, os.path.join("app", "fetchers"))
for f in WEB_FILES:
    place(f, "web")

# The __init__.py files are what actually make Python treat these as packages.
for pkg in ["app", os.path.join("app", "fetchers")]:
    os.makedirs(pkg, exist_ok=True)
    init = os.path.join(pkg, "__init__.py")
    if not os.path.exists(init):
        open(init, "w").close()
        print(f"  created         {init}")
        moved += 1
    else:
        print(f"  already there   {init}")
        skipped += 1

os.makedirs("data", exist_ok=True)

print(f"\n{moved} file(s) placed, {skipped} already correct.\n")

# Verify the structure actually imports before declaring success.
sys.path.insert(0, here)
try:
    from app import cli, config, db, detect, geo, refresh, server  # noqa: F401
    from app.fetchers import ats  # noqa: F401
    print("Import check passed. Structure is correct.\n")
    print("Next:")
    print("  python -m app.cli init")
    print("  python -m app.cli add-batch companies.txt")
    print("  python -m app.cli refresh")
    print("  uvicorn app.server:app --reload --port 8081")
except Exception as e:
    print(f"Import check FAILED: {type(e).__name__}: {e}")
    print("\nCurrent layout:")
    for root, dirs, files in os.walk(here):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        depth = root.replace(here, "").count(os.sep)
        if depth > 2:
            continue
        print(f"  {'  ' * depth}{os.path.basename(root) or '.'}/")
        for f in sorted(files):
            print(f"  {'  ' * (depth + 1)}{f}")
    sys.exit(1)
