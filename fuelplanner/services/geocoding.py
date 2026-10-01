import hashlib
import re
from dataclasses import asdict, dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from .errors import LocationNotFound, LocationOutsideUSA, RoutingUnavailable
from .http import Throttle, get_session
from .places import lookup_city_state, names_non_us_region

LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

# lower48, alaska, hawaii -- (min_lat, max_lat, min_lon, max_lon)
US_BOXES = [
    (24.3, 49.5, -125.0, -66.8),
    (51.0, 71.6, -179.2, -129.9),
    (18.8, 22.4, -160.4, -154.6),
]

# Nominatim's usage policy allows 1 req/s per application.
nominatim_throttle = Throttle(1.0)
NOT_FOUND_TTL = 60 * 60


@dataclass(frozen=True)
class Location:
    query: str
    label: str
    lat: float
    lon: float
    source: str  # coordinates / gazetteer / nominatim

    def as_dict(self):
        return asdict(self)


def in_usa(lat, lon):
    return any(a <= lat <= b and c <= lon <= d for a, b, c, d in US_BOXES)


def resolve_location(query, calls):
    query = (query or "").strip()
    if not query:
        raise LocationNotFound("Location must not be empty.")

    m = LATLON_RE.match(query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not in_usa(lat, lon):
            raise LocationOutsideUSA(f"'{query}' is not inside the USA (expected 'lat,lon').")
        return Location(query, f"{lat:.5f}, {lon:.5f}", lat, lon, "coordinates")

    # Without this, Nominatim (restricted to the US) happily returns e.g.
    # Toronto, Ohio for "Toronto, ON".
    if names_non_us_region(query):
        raise LocationOutsideUSA(f"'{query}' is not inside the USA.")

    place = lookup_city_state(query)
    if place:
        return Location(query, f"{place.name}, {place.state}", place.lat, place.lon, "gazetteer")

    key = "geocode:" + hashlib.sha1(query.lower().encode()).hexdigest()
    cached = cache.get(key)
    if cached == "not_found":
        raise LocationNotFound(f"Could not find a US location matching '{query}'.")
    if cached:
        return Location(**cached)

    try:
        location = nominatim_search(query, calls)
    except LocationNotFound:
        cache.set(key, "not_found", NOT_FOUND_TTL)
        raise
    cache.set(key, location.as_dict(), settings.FUEL_PLANNER["CACHE_TIMEOUT_SECONDS"])
    return location


def nominatim_search(query, calls):
    cfg = settings.FUEL_PLANNER
    nominatim_throttle.wait()
    calls.record("nominatim")
    try:
        resp = get_session().get(
            f"{cfg['NOMINATIM_URL']}/search",
            params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
            timeout=cfg["HTTP_TIMEOUT_SECONDS"],
        )
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingUnavailable(f"Geocoding service unavailable: {exc}") from exc

    if not results:
        raise LocationNotFound(f"Could not find a US location matching '{query}'.")
    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    if not in_usa(lat, lon):
        raise LocationOutsideUSA(f"'{query}' is not inside the USA.")
    return Location(query, results[0].get("display_name", query), lat, lon, "nominatim")
