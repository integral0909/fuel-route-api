from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .geo import chord_to_miles, miles_to_chord, resample_polyline, to_unit_xyz

STEP_MILES = 0.25


@dataclass(frozen=True)
class RouteStation:
    index: int  # row in the StationIndex
    route_mile: float
    offset_miles: float  # straight-line distance off the route
    price: float


def stations_along_route(route_lat, route_lon, route_miles, stations, corridor_miles):
    if not len(stations):
        return []

    lat, lon, marks = resample_polyline(route_lat, route_lon, STEP_MILES)
    # Geometry length is a bit off from OSRM's road distance; scale the mile
    # markers so they agree with the distance we do the fuel maths on.
    if marks[-1] > 0:
        marks *= route_miles / marks[-1]

    tree = cKDTree(to_unit_xyz(lat, lon))
    dist, nearest = tree.query(stations.xyz, distance_upper_bound=miles_to_chord(corridor_miles))
    hits = np.flatnonzero(np.isfinite(dist))
    offsets = chord_to_miles(dist[hits])

    found = [
        RouteStation(int(i), float(marks[nearest[i]]), float(off), float(stations.prices[i]))
        for i, off in zip(hits, offsets, strict=True)
    ]
    found.sort(key=lambda s: (s.route_mile, s.price))
    return found
