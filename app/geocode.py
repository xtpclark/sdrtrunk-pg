"""
Nominatim geocoder with City bounding box enforcement.

Rate-limited to 1 req/sec per Nominatim ToS.
In-memory address cache to avoid re-querying the same string.

Changes from v1:
- Only geocodes 'address' entities (not 'location' — too ambiguous)
- Appends city context to improve disambiguation
- bounded=1 forces results inside city bbox
- countrycodes=us to prevent overseas matches
- Validates result is inside city bbox

Usage:
    from app.geocode import geocode, geocode_call_entities
    lat, lon = geocode("100 Main Street") or (None, None)
    geocode_call_entities(call_id)
"""

import logging
import threading
import time
from typing import Optional, Tuple

import requests

from app.config import (
    NOMINATIM_URL, NOMINATIM_VIEWBOX,
    CITY_BBOX, CITY_GEOCODE_CONTEXT, CITY_CORRECTIONS,
)
from app.db import db

log = logging.getLogger(__name__)

# Bounding box — from city config
_BBOX_SW_LON, _BBOX_SW_LAT, _BBOX_NE_LON, _BBOX_NE_LAT = CITY_BBOX

# Nominatim viewbox format: left,top,right,bottom (lon_min, lat_max, lon_max, lat_min)
_VIEWBOX = f"{_BBOX_SW_LON},{_BBOX_NE_LAT},{_BBOX_NE_LON},{_BBOX_SW_LAT}"

# City context for geocoding disambiguation — from city config
_CITY_CONTEXT = CITY_GEOCODE_CONTEXT

# In-memory cache: normalized_address → (lat, lon) or None
# None is only stored for confirmed "no results" — not for transient errors.
_cache: dict[str, Optional[Tuple[float, float]]] = {}
_cache_lock = threading.Lock()

_last_request_time: float = 0.0
_rate_lock = threading.Lock()
_MIN_INTERVAL_SEC: float = 1.1  # slightly over 1s per Nominatim ToS


def _rate_limit():
    """Thread-safe rate limiter for Nominatim (1 req/sec)."""
    with _rate_lock:
        global _last_request_time
        elapsed = time.monotonic() - _last_request_time
        if elapsed < _MIN_INTERVAL_SEC:
            time.sleep(_MIN_INTERVAL_SEC - elapsed)
        _last_request_time = time.monotonic()


def _in_bbox(lat: float, lon: float) -> bool:
    """Validate result is inside the city bounding box."""
    return (_BBOX_SW_LAT <= lat <= _BBOX_NE_LAT and
            _BBOX_SW_LON <= lon <= _BBOX_NE_LON)


# ── Street name normalization ────────────────────────────────────────────
# Abbreviation patterns — only match STANDALONE abbreviations (not already expanded)
# Each tuple: (pattern, full_word) — pattern uses negative lookahead to avoid doubling
_ABBREV = [
    (r'\bave(?!nue)\b',       'Avenue'),
    (r'\bblvd(?!evard)\b',    'Boulevard'),
    (r'\bst(?!reet)\b',       'Street'),
    (r'\brd(?!oad)\b',        'Road'),
    (r'\bdr(?!ive)\b',        'Drive'),
    (r'\bln(?!ane)\b',        'Lane'),
    (r'\bct(?!ourt)\b',       'Court'),
    (r'\bhwy(?!ay)\b',        'Highway'),
    (r'\bpkwy\b',             'Parkway'),
    (r'\bpl(?!ace)\b',        'Place'),
    (r'\bter(?!race)\b',      'Terrace'),
    (r'\bn(?!orth)\b',        'North'),
    (r'\bs(?!outh)\b',        'South'),
    (r'\be(?!ast)\b',         'East'),
    (r'\bw(?!est)\b',         'West'),
    (r'\bnb\b',               'North'),
    (r'\bsb\b',               'South'),
    (r'\beb\b',               'East'),
    (r'\bwb\b',               'West'),
]

# Street name corrections — loaded from city config
# Street name corrections — loaded from city config (data/cities/{slug}/street_corrections.json)
_CORRECTIONS = CITY_CORRECTIONS

import re as _re

def _normalize_address(address: str) -> str:
    """
    Normalize abbreviations and fix known Whisper transcription errors.
    Corrections are applied as whole-phrase replacements BEFORE abbreviation
    expansion so we don't double-expand suffixes already in the correction.
    """
    s = address.strip()

    # Whisper often dictates numbers as "7-9-3-6" or "8.5-3" instead of "7936" or "853"
    # Collapse digit-dash-digit and digit-dot-digit sequences into solid numbers
    s = _re.sub(r'(\d)[.\-,]\s*(?=\d)', r'\1', s)
    sl = s.lower()

    # Apply known corrections — longest match first, stop after first match
    # to prevent shorter overlapping patterns from double-replacing.
    for wrong, right in sorted(_CORRECTIONS.items(), key=lambda x: -len(x[0])):
        if wrong in sl:
            s = _re.sub(_re.escape(wrong), right, s, flags=_re.IGNORECASE)
            sl = s.lower()
            break  # one correction per address — avoids substring double-match

    # Always run abbreviation expansion — patterns use negative lookaheads
    # to avoid doubling already-expanded words (e.g. "Avenue" won't match \bave(?!nue)\b)
    for pattern, replacement in _ABBREV:
        s = _re.sub(pattern, replacement, s, flags=_re.IGNORECASE)

    return s.strip()


def _is_intersection(address: str) -> bool:
    """Detect intersection format: "Street1 and Street2"."""
    return bool(_re.search(r'\band\b', address, _re.IGNORECASE))


def _geocode_intersection(street1: str, street2: str) -> Optional[Tuple[float, float]]:
    """
    Use Nominatim structured query for intersections, which handles
    cross-street lookups better than the freeform q= parameter.
    """
    _rate_limit()
    # Derive city/state from CITY_GEOCODE_CONTEXT (e.g. "Norfolk, VA")
    _ctx_parts = _CITY_CONTEXT.split(",")
    _ctx_city  = _ctx_parts[0].strip() if _ctx_parts else ""
    _ctx_state = _ctx_parts[1].strip() if len(_ctx_parts) > 1 else ""

    params = {
        "street": f"{street1} and {street2}",
        "city": _ctx_city,
        "state": _ctx_state,
        "country": "US",
        "format": "json",
        "limit": 3,
        "addressdetails": 0,
    }
    headers = {"User-Agent": "sdrtrunk-pg/1.0 scanner-radio-transcription"}
    try:
        resp = requests.get(f"{NOMINATIM_URL}/search", params=params,
                            headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        for r in results:
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
                if _in_bbox(lat, lon):
                    return (lat, lon)
            except (KeyError, ValueError):
                continue
    except Exception as exc:
        log.debug("Intersection geocode failed for %r/%r: %s", street1, street2, exc)
    return None


def _local_lookup(address: str) -> Optional[Tuple[float, float]]:
    """
    Fast local geocode against the address_db table loaded from city open data.
    Uses pg_trgm similarity for fuzzy matching — handles minor Whisper errors.
    Returns (lat, lon) if similarity >= 0.55, else None.
    """
    query_upper = address.upper().strip()
    if not query_upper or len(query_upper) < 5:
        return None

    sql = """
        SELECT lat, lon, full_address,
               similarity(full_address, %s) AS sim
        FROM address_db
        WHERE full_address %% %s
        ORDER BY sim DESC
        LIMIT 1
    """
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(sql, (query_upper, query_upper))
            row = cur.fetchone()
        if row and row["sim"] >= 0.55:
            log.info("Local geocode %r → %r (sim=%.2f) (%.5f, %.5f)",
                     address, row["full_address"], row["sim"], row["lat"], row["lon"])
            return (row["lat"], row["lon"])
    except Exception as exc:
        log.debug("Local lookup failed for %r: %s", address, exc)
    return None


def _local_intersection(street1: str, street2: str) -> Optional[Tuple[float, float]]:
    """
    Find intersection of two streets by averaging all address points on street1
    that are nearest to street2 addresses. Fast approximation using DB.
    """
    s1 = street1.upper().strip()
    s2 = street2.upper().strip()
    if not s1 or not s2:
        return None

    # Find the midpoint of addresses on street1 that are closest to any address on street2
    sql = """
        SELECT avg(a1.lat) as lat, avg(a1.lon) as lon
        FROM address_db a1
        WHERE a1.full_street %% %s
          AND EXISTS (
              SELECT 1 FROM address_db a2
              WHERE a2.full_street %% %s
                AND ST_DWithin(
                    ST_SetSRID(ST_MakePoint(a1.lon, a1.lat), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(a2.lon, a2.lat), 4326)::geography,
                    200
                )
          )
        HAVING count(*) > 0
    """
    try:
        with db() as conn:
            cur = conn.cursor()
            # Use street_name column for cleaner matching
            cur.execute("""
                SELECT avg(a1.lat) as lat, avg(a1.lon) as lon, count(*) as cnt
                FROM address_db a1
                JOIN address_db a2 ON (
                    similarity(a2.full_street, %s) > 0.5
                    AND ST_DWithin(
                        ST_SetSRID(ST_MakePoint(a1.lon, a1.lat), 4326)::geography,
                        ST_SetSRID(ST_MakePoint(a2.lon, a2.lat), 4326)::geography,
                        150
                    )
                )
                WHERE similarity(a1.full_street, %s) > 0.5
                HAVING count(*) > 0
            """, (s2, s1))
            row = cur.fetchone()
        if row and row["cnt"] and row["lat"]:
            log.info("Local intersection %r & %r → (%.5f, %.5f) (%d pts)",
                     street1, street2, row["lat"], row["lon"], row["cnt"])
            return (float(row["lat"]), float(row["lon"]))
    except Exception as exc:
        log.debug("Local intersection failed %r & %r: %s", street1, street2, exc)
    return None


def geocode(address: str, city_context: bool = True) -> Optional[Tuple[float, float]]:
    """
    Geocode an address string, biased and bounded to the configured city.

    Returns (lat, lon) or None. Results are cached in-process.
    Handles:
      - Street addresses: "827 Main Avenue"
      - Intersections:    "Church Street and Johnson Avenue"
      - Normalizes abbreviations and known Whisper transcription errors
    """
    address = _normalize_address(address.strip())
    if not address or len(address) < 5:
        return None

    cache_key = address.lower()
    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]

    headers = {"User-Agent": "sdrtrunk-pg/1.0 scanner-radio-transcription"}

    def _store(val):
        """Thread-safe cache write."""
        with _cache_lock:
            _cache[cache_key] = val

    # ── Intersection path ─────────────────────────────────────────────
    if _is_intersection(address):
        parts = _re.split(r'\band\b', address, flags=_re.IGNORECASE, maxsplit=1)
        if len(parts) == 2:
            s1, s2 = parts[0].strip(), parts[1].strip()
            result = _local_intersection(s1, s2)
            if result:
                _store(result)
                return result
            result = _geocode_intersection(s1, s2)
            if result:
                log.info("Nominatim intersection %r → (%.5f, %.5f)", address, *result)
                _store(result)
                return result

    # ── Local DB lookup (fast, no rate limit) ────────────────────────
    result = _local_lookup(address)
    if result:
        _store(result)
        return result

    # ── Nominatim fallback (rate-limited) ────────────────────────────
    query = f"{address}, {_CITY_CONTEXT}" if city_context else address

    _rate_limit()
    params = {
        "q":            query,
        "format":       "json",
        "limit":        3,
        "addressdetails": 0,
        "countrycodes": "us",
        "viewbox":      _VIEWBOX,
        "bounded":      1,
    }

    try:
        resp = requests.get(f"{NOMINATIM_URL}/search", params=params,
                            headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
    except Exception as exc:
        # Transient error — do NOT cache None so we can retry later
        log.error("Nominatim request failed for %r: %s", address, exc)
        return None

    # If bounded returns nothing, retry without bounding box
    if not results:
        params["bounded"] = 0
        try:
            resp = requests.get(f"{NOMINATIM_URL}/search", params=params,
                                headers=headers, timeout=10)
            results = resp.json()
        except Exception:
            return None  # transient — don't cache

    for r in results:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
            if _in_bbox(lat, lon):
                log.info("Geocoded %r → (%.5f, %.5f)", address, lat, lon)
                _store((lat, lon))
                return (lat, lon)
        except (KeyError, ValueError):
            continue

    # Confirmed no results — safe to cache as None
    log.debug("Nominatim: no result for %r", address)
    _store(None)
    return None


def geocode_call_entities(call_id: int):
    """
    Geocode 'address' entities for a call. Skips 'location' type —
    too ambiguous for reliable geocoding.
    Updates call_entities.lat, .lon, .geom.
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, value, entity_type
            FROM call_entities
            WHERE call_id = %s
              AND entity_type = 'address'
              AND lat IS NULL
            """,
            (call_id,),
        )
        entities = cur.fetchall()

    if not entities:
        return

    updates = []
    for ent in entities:
        result = geocode(ent["value"])
        if result:
            updates.append((result[0], result[1], ent["id"]))
        else:
            log.debug("No geocode for %r (call %d)", ent["value"], call_id)

    if not updates:
        return

    with db() as conn:
        cur = conn.cursor()
        for lat, lon, ent_id in updates:
            cur.execute(
                """
                UPDATE call_entities
                SET lat  = %s,
                    lon  = %s,
                    geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                WHERE id = %s
                """,
                (lat, lon, lon, lat, ent_id),
            )

    log.info("Geocoded %d/%d address entities for call %d",
             len(updates), len(entities), call_id)
