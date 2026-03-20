import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── City configuration ────────────────────────────────────────────────────
# Loaded once at startup. All city-specific values come from here.
# Override with CITY_CONFIG env var to point at a different city.

def _load_city_config() -> dict:
    try:
        import yaml
    except ImportError:
        # yaml not installed — use a minimal inline default so the app still starts
        return {}

    config_path = Path(os.getenv(
        "CITY_CONFIG",
        Path(__file__).resolve().parent.parent / "data/cities/norfolk-va/config.yaml"
    ))
    if not config_path.exists():
        return {}

    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    # Load street corrections from sibling JSON file
    corrections_path = config_path.parent / "street_corrections.json"
    cfg["street_corrections"] = json.loads(corrections_path.read_text()) \
        if corrections_path.exists() else {}

    # Load landmarks from sibling JSON file
    landmarks_path = config_path.parent / "landmarks.json"
    cfg["landmarks"] = json.loads(landmarks_path.read_text()) \
        if landmarks_path.exists() else []

    return cfg


CITY = _load_city_config()

# Convenience accessors with safe defaults (so existing code doesn't break
# if CITY is empty — e.g. yaml not installed yet)
CITY_NAME           = CITY.get("name", "Norfolk, VA")
CITY_MAP_CENTER     = CITY.get("map_center", [36.8508, -76.2859])
CITY_MAP_ZOOM       = CITY.get("map_zoom", 12)
CITY_BBOX           = CITY.get("bbox", [-76.9, 36.5, -75.9, 37.3])  # [sw_lon, sw_lat, ne_lon, ne_lat]
CITY_GEOCODE_CONTEXT= CITY.get("geocode_context", "Norfolk, VA")
CITY_ENTITY_CONTEXT = CITY.get("entity_context", "Norfolk, Virginia")
CITY_CORRECTIONS    = CITY.get("street_corrections", {})
CITY_LANDMARKS      = CITY.get("landmarks", [])

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/sdrtrunk")

# Archive root — MP3s stored as {ARCHIVE_ROOT}/{YYYYMMDD}/{tg}/{filename}.mp3
ARCHIVE_ROOT = Path(os.getenv("ARCHIVE_ROOT", "/var/sdrtrunk/archive"))

# Merge output — merged MP3s stored here
MERGE_ROOT = Path(os.getenv("MERGE_ROOT", "/var/sdrtrunk/merges"))

# API key — SDRTrunk is configured to send this
API_KEY = os.getenv("API_KEY", "changeme")

# Whisper model: tiny, base, small, medium, large
# 'base' is recommended for scanner audio — narrow vocabulary, fast
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

# OpenAI — for embeddings and entity extraction
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# Google Gemini — preferred embedding + entity provider
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Embedding/entity provider: gemini | openai | local
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "gemini")
ENTITY_PROVIDER    = os.getenv("ENTITY_PROVIDER", "gemini")

# Alert webhook — POST alerts here (e.g. OpenClaw webhook URL)
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")

# Nominatim geocoding
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
# Hampton Roads bounding box for geocoding bias: sw_lon, sw_lat, ne_lon, ne_lat
NOMINATIM_VIEWBOX = os.getenv("NOMINATIM_VIEWBOX", "-76.5,36.5,-75.9,37.2")

# Timezone for display
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

# Flask
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
PORT = int(os.getenv("PORT", 5010))
