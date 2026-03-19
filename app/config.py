import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

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
