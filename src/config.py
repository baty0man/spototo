"""Environment-driven configuration for the Yoto pixel pipeline."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent


def _path(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var, str(REPO_ROOT / default))).expanduser()


class Config:
    YOTO_CLIENT_ID = os.environ.get("YOTO_CLIENT_ID", "")
    YOTO_CARD_TITLE = os.environ.get("YOTO_CARD_TITLE", "Kids Playlist")
    YOTO_CARD_ID = os.environ.get("YOTO_CARD_ID", "")

    REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")

    # generate: LLM subject + AI-generated pixel art (default).
    # yoto-library: deterministic keyword match against Yoto's own official
    #   icon library -- no AI involved at all. Falls back to no icon if no
    #   good match, never silently falls back to generation.
    # yoto-library-ai: library first; paid fallback needs token + positive cap.
    # none: no icon step at all.
    ICON_SOURCE = os.environ.get("ICON_SOURCE", "generate")
    ICON_CACHE_DIR = _path("ICON_CACHE_DIR", "data/icons")
    ICON_AI_MAX_GENERATIONS = int(os.environ.get("ICON_AI_MAX_GENERATIONS", "0"))

    WATCH_DIR = _path("WATCH_DIR", "data/watch")
    PROCESSED_DIR = _path("PROCESSED_DIR", "data/processed")
    STATE_FILE = _path("STATE_FILE", "data/state.json")

    STABLE_WAIT_SECONDS = float(os.environ.get("STABLE_WAIT_SECONDS", "3"))

    YOTO_AUTH_BASE = os.environ.get("YOTO_AUTH_BASE", "https://login.yotoplay.com")
    YOTO_API_BASE = os.environ.get("YOTO_API_BASE", "https://api.yotoplay.com")
    YOTO_AUDIENCE = os.environ.get("YOTO_AUDIENCE", "https://api.yotoplay.com")
    YOTO_SCOPE = os.environ.get(
        "YOTO_SCOPE",
        "offline_access user:content:manage user:content:view user:icons:manage",
    )

    @classmethod
    def validate(cls):
        from icon_resolver import validate_icon_config
        validate_icon_config(cls)
        required = ["YOTO_CLIENT_ID"]
        if cls.ICON_SOURCE == "generate":
            required.append("REPLICATE_API_TOKEN")
        missing = [name for name in required if not getattr(cls, name)]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
        cls.WATCH_DIR.mkdir(parents=True, exist_ok=True)
        cls.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        cls.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
