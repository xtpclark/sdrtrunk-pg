"""
Nominatim geocoder with Hampton Roads bounding box enforcement.

Rate-limited to 1 req/sec per Nominatim ToS.
In-memory address cache to avoid re-querying the same string.

Changes from v1:
- Only geocodes 'address' entities (not 'location' — too ambiguous)
- Appends ", Norfolk, VA" context to improve disambiguation
- bounded=1 forces results inside Hampton Roads bbox
- countrycodes=us to prevent overseas matches
- Validates result is actually inside Hampton Roads bbox

Usage:
    from app.geocode import geocode, geocode_call_entities
    lat, lon = geocode("100 Main Street") or (None, None)
    geocode_call_entities(call_id)
"""

import logging
import time
from typing import Optional, Tuple

import requests

from app.config import NOMINATIM_URL, NOMINATIM_VIEWBOX
from app.db import db

log = logging.getLogger(__name__)

# Hampton Roads bounding box: sw_lon, sw_lat, ne_lon, ne_lat
_BBOX_SW_LON = -76.9
_BBOX_SW_LAT = 36.5
_BBOX_NE_LON = -75.9
_BBOX_NE_LAT = 37.3

# Nominatim viewbox format: left,top,right,bottom (lon_min, lat_max, lon_max, lat_min)
_VIEWBOX = f"{_BBOX_SW_LON},{_BBOX_NE_LAT},{_BBOX_NE_LON},{_BBOX_SW_LAT}"

# City context appended to improve disambiguation
_CITY_CONTEXT = "Norfolk, VA"

# In-memory cache: normalized_address → (lat, lon) or None
_cache: dict[str, Optional[Tuple[float, float]]] = {}

_last_request_time: float = 0.0
_MIN_INTERVAL_SEC: float = 1.1  # slightly over 1s to be safe


def _rate_limit():
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - elapsed)
    _last_request_time = time.monotonic()


def _in_hampton_roads(lat: float, lon: float) -> bool:
    """Validate result is inside Hampton Roads bounding box."""
    return (_BBOX_SW_LAT <= lat <= _BBOX_NE_LAT and
            _BBOX_SW_LON <= lon <= _BBOX_NE_LON)


def geocode(address: str, city_context: bool = True) -> Optional[Tuple[float, float]]:
    """
    Geocode an address string, biased and bounded to Hampton Roads.

    Returns (lat, lon) or None. Results are cached in-process.
    """
    address = address.strip()
    if not address or len(address) < 4:
        return None

    # Append city context for disambiguation
    query = f"{address}, {_CITY_CONTEXT}" if city_context else address

    cache_key = query.lower()
    if cache_key in _cache:
        return _cache[cache_key]

    _rate_limit()

    params = {
        "q": query,
        "format": "json",
        "limit": 3,          # get a few candidates so we can pick the best
        "addressdetails": 0,
        "countrycodes": "us",
        "viewbox": _VIEWBOX,
        "bounded": 1,        # ONLY return results inside the viewbox
    }

    headers = {"User-Agent": "sdrtrunk-pg/1.0 scanner-radio-transcription"}

    try:
        resp = requests.get(
            f"{NOMINATIM_URL}/search",
            params=params,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as exc:
        log.error("Nominatim request failed for %r: %s", address, exc)
        _cache[cache_key] = None
        return None

    if not results:
        # Try again without bounded — sometimes the bbox is too tight
        # for cross-street or named location queries
        params["bounded"] = 0
        try:
            resp = requests.get(
                f"{NOMINATIM_URL}/search",
                params=params,
                headers=headers,
                timeout=10,
            )
            results = resp.json()
        except Exception:
            pass

    # Find first result inside Hampton Roads
    for r in results:
        try:
            lat = float(r["lat"])
            lon = float(r["lon"])
            if _in_hampton_roads(lat, lon):
                log.info("Geocoded %r → (%.5f, %.5f)", address, lat, lon)
                _cache[cache_key] = (lat, lon)
                return (lat, lon)
        except (KeyError, ValueError):
            continue

    log.debug("Nominatim: no Hampton Roads result for %r", address)
    _cache[cache_key] = None
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
            log.debug("No Hampton Roads geocode for %r (call %d)", ent["value"], call_id)

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
