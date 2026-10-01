from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache

from .errors import NoRouteFound, RoutingUnavailable
from .geo import METERS_PER_MILE
from .http import get_session


@dataclass(frozen=True)
class Route:
    distance_miles: float
    duration_hours: float
    lat: np.ndarray
    lon: np.ndarray


def fetch_route(start_lat, start_lon, end_lat, end_lon, calls):
    cfg = settings.FUEL_PLANNER
    coords = f"{start_lon:.6f},{start_lat:.6f};{end_lon:.6f},{end_lat:.6f}"
    key = f"osrm:{coords}"

    data = cache.get(key)
    if data is None:
        data = _request_route(coords, calls, cfg)
        cache.set(key, data, cfg["CACHE_TIMEOUT_SECONDS"])

    pts = np.asarray(data["coordinates"], dtype=float)  # [lon, lat] pairs
    return Route(
        distance_miles=data["distance_m"] / METERS_PER_MILE,
        duration_hours=data["duration_s"] / 3600,
        lat=pts[:, 1],
        lon=pts[:, 0],
    )


def _request_route(coords, calls, cfg):
    calls.record("osrm")
    try:
        resp = get_session().get(
            f"{cfg['OSRM_BASE_URL']}/route/v1/driving/{coords}",
            params={"overview": "full", "geometries": "geojson", "steps": "false"},
            timeout=cfg["HTTP_TIMEOUT_SECONDS"],
        )
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingUnavailable(f"Routing service unavailable: {exc}") from exc

    if payload.get("code") != "Ok" or not payload.get("routes"):
        msg = payload.get("message") or "No drivable route between the locations."
        if resp.status_code >= 500:
            raise RoutingUnavailable(f"Routing service error: {msg}")
        raise NoRouteFound(msg)

    route = payload["routes"][0]
    return {
        "distance_m": route["distance"],
        "duration_s": route["duration"],
        "coordinates": route["geometry"]["coordinates"],
    }
