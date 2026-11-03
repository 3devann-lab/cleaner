"""Central configuration.

Every secret comes from environment variables (Railway "Variables" tab, or a
local .env file that is git-ignored). Nothing sensitive is ever hard-coded.
"""
import os
import re

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is only needed for local development
    pass


def _ids(raw: str) -> frozenset:
    return frozenset(int(x) for x in re.split(r"[,\s]+", raw or "") if x.isdigit())


def _flag(raw: str, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Comma/space separated Discord user IDs that fully control the bot.
OWNER_IDS = _ids(os.getenv("OWNER_IDS", ""))

# Optional: sync slash commands instantly to one test server while developing.
_dev = (os.getenv("DEV_GUILD_ID") or "").strip()
DEV_GUILD_ID = int(_dev) if _dev.isdigit() else None

SYNC_ON_START = _flag(os.getenv("SYNC_COMMANDS"), True)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ------------------------------------------------------------------ dashboard
# The web dashboard only starts when DASH_PASSWORD is set (fails closed).
DASH_PASSWORD = os.getenv("DASH_PASSWORD", "")
DASH_MIN_PASSWORD_LENGTH = 10
PORT = int(os.getenv("PORT", "8080") or 8080)  # Railway injects PORT automatically
DASH_SESSION_HOURS = max(1, min(int(os.getenv("DASH_SESSION_HOURS", "12") or 12), 72))
# Optional allow-list: only these server IDs can be managed from the dashboard.
DASH_GUILD_IDS = _ids(os.getenv("DASH_GUILD_IDS", ""))

# Groq (AI auto-organizer). Optional: without a key the editor still works, only the AI button is off.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"


def validate() -> None:
    """Fail fast, with a readable message, if required variables are missing."""
    missing = [
        name
        for name, value in (("DISCORD_TOKEN", TOKEN), ("DATABASE_URL", DATABASE_URL))
        if not value
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in Railway -> your bot service -> Variables."
        )
    if not OWNER_IDS:
        print(
            "WARNING: OWNER_IDS is empty. Bot-owner commands (/owner ...) will be "
            "unavailable until you set it."
        )
    if not DASH_PASSWORD:
        print("INFO: DASH_PASSWORD is not set, so the web dashboard is disabled.")
    elif len(DASH_PASSWORD) < DASH_MIN_PASSWORD_LENGTH:
        print(
            f"WARNING: DASH_PASSWORD is shorter than {DASH_MIN_PASSWORD_LENGTH} characters, "
            "so the web dashboard is DISABLED. Use a longer password."
        )


def dashboard_enabled() -> bool:
    return len(DASH_PASSWORD) >= DASH_MIN_PASSWORD_LENGTH
