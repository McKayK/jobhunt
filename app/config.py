import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("JOBHUNT_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "jobhunt.db"

# --- Search criteria (seed defaults only; the UI owns these after first run) --
HOME_ZIP = os.getenv("HOME_ZIP", "84042")          # Lindon, UT
RADIUS_MILES = float(os.getenv("RADIUS_MILES", "30"))
INCLUDE_REMOTE = os.getenv("INCLUDE_REMOTE", "1") == "1"
# Jobs whose location string won't geocode at all. Kept by default and flagged
# in the UI, so an unparseable-but-local posting isn't silently lost. Set to 0
# for a stricter board.
KEEP_UNKNOWN_LOCATIONS = os.getenv("KEEP_UNKNOWN_LOCATIONS", "1") == "1"

# Comma-separated. Empty TITLE_INCLUDE means "keep everything".
TITLE_INCLUDE = [s.strip().lower() for s in os.getenv("TITLE_INCLUDE", "").split(",") if s.strip()]
TITLE_EXCLUDE = [s.strip().lower() for s in os.getenv(
    "TITLE_EXCLUDE",
    "intern,internship,senior director,vice president,vp,chief,principal"
).split(",") if s.strip()]

# --- Fetch behaviour -------------------------------------------------------
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "jobhunt/0.3 (personal job search tool; contact: local)"
)
# A normal browser UA for the few sites that refuse obvious bots (search
# engines used by the board finder). ATS APIs get the honest UA above.
BROWSER_UA = os.getenv(
    "BROWSER_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
)
# Nominatim asks for <=1 req/sec and a real contact string.
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
GEOCODE_DELAY = float(os.getenv("GEOCODE_DELAY", "1.1"))

# --- AI (optional) -----------------------------------------------------------
# Resume tailoring and the AI step of "Find job board" need ONE of these.
# Keys can also be pasted into Settings in the UI (stored in the database);
# an environment variable always wins over a pasted key.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Override the default models if you like.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# --- Server ----------------------------------------------------------------
PORT = int(os.getenv("PORT", "8081"))
