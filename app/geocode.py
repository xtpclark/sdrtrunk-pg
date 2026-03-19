"""
Nominatim geocoder with Hampton Roads viewbox bias.

Rate-limited to 1 req/sec per Nominatim ToS.
In-memory address cache to avoid re-querying the same string.

Usage:
    from app.geocode import geocode, geocode_call_entities

    lat, lon = geocode("100 Main Street, Norfolk VA") or (None, None)
    geocode_call_entities(call_id)
"""

import logging
import time
from typing import Optional, Tuple

import requests

from app.config import NOMINATIM_URL, NOMINATIM_VIEWBOX
from app.db import db

log = logging.getLogger(__name__)

# In-memory cache: address_string → (lat, lon) or None
_cache: dict[str, Optional[Tuple[float, float]]] = {}

# Rate limiter state
_last_request_time: float = 0.0
_MIN_INTERVAL_SEC: float = 1.0  # Nominatim ToS: max 1 req/sec


def _rate_limit():
    """Sleep if needed to honour 1 req/sec limit."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - elapsed)
    _last_request_time = time.monotonic()


def geocode(address: str) -> Optional[Tuple[float, float]]:
    """
    Geocode address string with Hampton Roads viewbox bias.

    Returns (lat, lon) tuple or None if not found / on error.
    Results are cached in-process.
    """
    key = address.strip().lower()
    if not key:
        return None

    if key in _cache:
        return _cache[key]

    _rate_limit()

    # Parse viewbox: "-76.5,36.5,-75.9,37.2" → "left,top,right,bottom" for Nominatim
    # Nominatim viewbox format: left,top,right,bottom  (lon_min, lat_max, lon_max, lat_min)
    try:
        parts = [float(x) for x in NOMINATIM_VIEWBOX.split(",")]
        # Our env: sw_lon, sw_lat, ne_lon, ne_lat
        sw_lon, sw_lat, ne_lon, ne_lat = parts
        viewbox = f"{sw_lon},{ne_lat},{ne_lon},{sw_lat}"
    except Exception:
        viewbox = None

    params = {
        "q": address,
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
    }
    if viewbox:
        params["viewbox"] = viewbox
        params["bounded"] = 0  # prefer viewbox but fall back to global

    headers = {"User-Agent": "sdrtrunk-pg/1.0 (scanner radio transcription)"}

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
        _cache[key] = None
        return None

    if not results:
        log.debug("Nominatim: no results for %r", address)
        _cache[key] = None
        return None

    try:
        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
    except (KeyError, ValueError, IndexError) as exc:
        log.error("Nominatim bad response for %r: %s", address, exc)
        _cache[key] = None
        return None

    log.debug("Geocoded %r → (%.5f, %.5f)", address, lat, lon)
    _cache[key] = (lat, lon)
    return (lat, lon)


def geocode_call_entities(call_id: int):
    """
    Geocode all address-type entities for call_id.
    Updates call_entities.lat, .lon, and .geom for each address found.
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, value
            FROM call_entities
            WHERE call_id = %s
              AND entity_type IN ('address', 'location')
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

    log.info(
        "Geocoded %d/%d entities for call %d",
        len(updates),
        len(entities),
        call_id,
    )
