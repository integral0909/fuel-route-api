import hashlib
import json
import logging
import time

import numpy as np
from django.conf import settings
from django.core.cache import cache

from .corridor import stations_along_route
from .errors import NoFeasibleFuelPlan
from .geo import cumulative_miles
from .geocoding import resolve_location
from .http import CallCounter
from .optimizer import InfeasibleRoute, Stop, plan_fuel_stops
from .osrm import fetch_route
from .station_index import get_station_index

log = logging.getLogger(__name__)

MAP_POINT_SPACING_MILES = 0.5


def plan_trip(start, finish, start_fuel_fraction=0.0, corridor_miles=None, stop_penalty=None):
    cfg = settings.FUEL_PLANNER
    t0 = time.perf_counter()
    calls = CallCounter()
    corridor = cfg["DEFAULT_CORRIDOR_MILES"] if corridor_miles is None else corridor_miles
    penalty = cfg["DEFAULT_STOP_PENALTY"] if stop_penalty is None else stop_penalty
    tank_range, mpg = cfg["VEHICLE_RANGE_MILES"], cfg["VEHICLE_MPG"]

    origin = resolve_location(start, calls)
    dest = resolve_location(finish, calls)

    key_src = [origin.lat, origin.lon, dest.lat, dest.lon, start_fuel_fraction, corridor, penalty]
    cache_key = "plan:" + hashlib.sha1(json.dumps(key_src).encode()).hexdigest()
    result = cache.get(cache_key)
    if result is not None:
        # different spellings can resolve to the same coords, so echo this request's inputs
        result["start"], result["finish"] = origin.as_dict(), dest.as_dict()
        result["meta"].update(
            cache="hit",
            external_api_calls=len(calls),
            external_api_call_names=calls.calls,
            elapsed_ms=_ms_since(t0),
        )
        return result

    route = fetch_route(origin.lat, origin.lon, dest.lat, dest.lon, calls)
    index = get_station_index()
    nearby = stations_along_route(route.lat, route.lon, route.distance_miles, index, corridor)

    try:
        plan = _optimize(route, nearby, start_fuel_fraction, penalty)
        cheapest = _optimize(route, nearby, start_fuel_fraction, 0.0) if penalty > 0 else plan
    except InfeasibleRoute as exc:
        raise _no_plan_error(exc, route, index, corridor, start_fuel_fraction) from exc

    stops = [_stop_json(n, p, index) for n, p in enumerate(plan.purchases, start=1)]
    result = {
        "start": origin.as_dict(),
        "finish": dest.as_dict(),
        "route": {
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_hours, 2),
        },
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": round(plan.trip_cost, 2),
            "fuel_cost_at_stops": round(plan.total_cost, 2),
            "assumed_start_fuel": _assumed_fuel_json(plan, index),
            "gallons_purchased": round(plan.total_gallons, 2),
            "gallons_used": round(route.distance_miles / mpg, 2),
            "number_of_stops": len(stops),
            "average_price_paid": round(plan.total_cost / plan.total_gallons, 4)
            if plan.total_gallons
            else None,
            "stations_considered": len(nearby),
            "cheapest_possible": {
                "total_fuel_cost": round(cheapest.trip_cost, 2),
                "number_of_stops": len(cheapest.purchases),
            },
        },
        "assumptions": {
            "mpg": mpg,
            "tank_range_miles": tank_range,
            "tank_capacity_gallons": tank_range / mpg,
            "start_fuel_gallons": round(plan.start_fuel_miles / mpg, 2),
            "arrival_fuel_gallons": round(plan.arrival_fuel_gallons, 2),
            "corridor_miles": corridor,
            "stop_penalty_usd": penalty,
            "notes": _notes(plan),
        },
        "map": {"geojson": _geojson(route, origin, dest, stops)},
        "meta": {
            "cache": "miss",
            "external_api_calls": len(calls),
            "external_api_call_names": calls.calls,
            "elapsed_ms": _ms_since(t0),
        },
    }
    cache.set(cache_key, result, cfg["CACHE_TIMEOUT_SECONDS"])
    log.info(
        "%s -> %s: %.0f mi, %d stops, $%.2f, %d ext calls, %s ms",
        origin.label,
        dest.label,
        route.distance_miles,
        len(stops),
        plan.trip_cost,
        len(calls),
        result["meta"]["elapsed_ms"],
    )
    return result


def _optimize(route, nearby, start_fuel_fraction, stop_penalty):
    cfg = settings.FUEL_PLANNER
    return plan_fuel_stops(
        [Stop(key=s, mile=s.route_mile, price=s.price) for s in nearby],
        route.distance_miles,
        cfg["VEHICLE_RANGE_MILES"],
        cfg["VEHICLE_MPG"],
        start_fuel_fraction * cfg["VEHICLE_RANGE_MILES"],
        stop_penalty,
    )


def _feasible(route, index, corridor, start_fuel_fraction):
    nearby = stations_along_route(route.lat, route.lon, route.distance_miles, index, corridor)
    try:
        _optimize(route, nearby, start_fuel_fraction, 0.0)
    except InfeasibleRoute:
        return False
    return True


def _no_plan_error(exc, route, index, corridor, start_fuel_fraction):
    # Only suggest changes we've checked would actually produce a plan; much of the
    # West Coast has no stations in the price file, and no setting fixes that.
    cfg = settings.FUEL_PLANNER
    max_corridor = cfg["MAX_CORRIDOR_MILES"]
    suggestions = []
    if start_fuel_fraction < 1 and _feasible(route, index, corridor, 1.0):
        suggestions.append({"start_fuel": 1.0})
    if corridor < max_corridor and _feasible(route, index, max_corridor, start_fuel_fraction):
        suggestions.append({"corridor_miles": max_corridor})

    if suggestions:
        hint = (
            "This works with "
            + " or ".join(", ".join(f"{k}={v:g}" for k, v in s.items()) for s in suggestions)
            + "."
        )
    else:
        hint = (
            "The fuel price data has no stations along that stretch, so this route can't be "
            f"planned with a {cfg['VEHICLE_RANGE_MILES']:.0f}-mile range."
        )
    return NoFeasibleFuelPlan(
        f"{exc} {hint}",
        gap={"from_mile": round(exc.gap_start_mile, 1), "to_mile": round(exc.gap_end_mile, 1)},
        suggestions=suggestions,
    )


def _assumed_fuel_json(plan, index):
    if not plan.assumed_fuel_stop:
        return None
    station = plan.assumed_fuel_stop.key
    rec = index.records[station.index]
    return {
        "gallons": round(plan.assumed_fuel_gallons, 2),
        "price_per_gallon": round(station.price, 4),
        "cost": round(plan.assumed_fuel_cost, 2),
        "priced_at": {k: rec[k] for k in ("opis_id", "name", "city", "state")},
    }


def _ms_since(t0):
    return round((time.perf_counter() - t0) * 1000, 1)


def _stop_json(seq, purchase, index):
    rs = purchase.stop.key
    rec = index.records[rs.index]
    return {
        "sequence": seq,
        "opis_id": rec["opis_id"],
        "name": rec["name"],
        "address": rec["address"],
        "city": rec["city"],
        "state": rec["state"],
        "lat": rec["latitude"],
        "lon": rec["longitude"],
        "price_per_gallon": round(rs.price, 4),
        "route_mile": round(rs.route_mile, 1),
        "distance_from_route_miles": round(rs.offset_miles, 1),
        "fuel_on_arrival_gallons": round(purchase.fuel_on_arrival_gallons, 2),
        "gallons": round(purchase.gallons, 2),
        "cost": round(purchase.cost, 2),
    }


def _notes(plan):
    notes = [
        "total_fuel_cost = fuel_cost_at_stops + assumed_start_fuel.cost. Fuel you declared "
        "with start_fuel isn't priced.",
        "Stations are geocoded to their city, "
        "so route_mile and distance_from_route_miles are approximate.",
        "Where the price file lists a station more than once, its lowest price is used.",
        "Stops minimise fuel cost + stop_penalty_usd per stop. summary.cheapest_possible is the "
        "plan with no penalty.",
    ]
    if plan.assumed_fuel_stop:
        notes.append(
            f"Start fuel was too low to reach a station, so {plan.assumed_fuel_gallons:.2f} gal "
            "more was assumed, priced at the first station on the route."
        )
    return notes


def _geojson(route, origin, dest, stops):
    # Full OSRM geometry can be 30k+ points; one every half mile is plenty to draw.
    cum = cumulative_miles(route.lat, route.lon)
    buckets = np.floor(cum / MAP_POINT_SPACING_MILES)
    keep = np.flatnonzero(np.diff(buckets, prepend=-1) > 0)
    if keep[-1] != len(cum) - 1:
        keep = np.append(keep, len(cum) - 1)
    line = np.round(np.column_stack((route.lon[keep], route.lat[keep])), 5).tolist()

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line},
            "properties": {"kind": "route", "distance_miles": round(route.distance_miles, 1)},
        },
        _point(origin.lon, origin.lat, kind="start", label=origin.label),
        _point(dest.lon, dest.lat, kind="finish", label=dest.label),
    ]
    for s in stops:
        props = {k: v for k, v in s.items() if k not in ("lat", "lon")}
        features.append(_point(s["lon"], s["lat"], kind="fuel_stop", **props))
    return {"type": "FeatureCollection", "features": features}


def _point(lon, lat, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }
