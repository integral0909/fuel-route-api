from decimal import Decimal
from unittest import mock

import numpy as np
import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from fuelplanner.models import FuelStation
from fuelplanner.services.corridor import stations_along_route
from fuelplanner.services.errors import NoRouteFound
from fuelplanner.services.station_index import get_station_index, reset_station_index

# Straight line along lat 40, roughly Denver to Indianapolis.
ROUTE_LON = np.linspace(-104.0, -85.2, 400)
ROUTE_LAT = np.full_like(ROUTE_LON, 40.0)
ROUTE_METERS = 1_600_000.0  # ~994 mi


def fake_osrm_response(*args, **kwargs):
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {
        "code": "Ok",
        "routes": [
            {
                "distance": ROUTE_METERS,
                "duration": 15 * 3600,
                "geometry": {
                    "type": "LineString",
                    "coordinates": np.column_stack((ROUTE_LON, ROUTE_LAT)).tolist(),
                },
            }
        ],
    }
    return resp


def station(opis_id, lon, price, lat=40.0):
    return FuelStation(
        opis_id=opis_id,
        name=f"Station {opis_id}",
        address="I-70",
        city="Town",
        state="KS",
        price=Decimal(str(price)),
        latitude=lat,
        longitude=lon,
        geocode_source="census_place",
    )


@override_settings(
    CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tests"}
    }
)
class RoutePlanApiTests(TestCase):
    def setUp(self):
        cache.clear()
        FuelStation.objects.bulk_create(
            [
                station(1, -103.9, 3.90),  # near start, pricey
                station(2, -101.0, 3.10),  # cheap, ~160 mi
                station(3, -97.0, 3.60),
                station(4, -94.5, 2.95),  # cheapest, ~660 mi
                station(5, -90.0, 3.80),
                station(6, -99.0, 2.50, lat=41.5),  # cheap but ~100 mi off route
            ]
        )
        reset_station_index()
        self.url = reverse("route-plan")

    def tearDown(self):
        reset_station_index()

    def _get(self, **params):
        with mock.patch(
            "fuelplanner.services.http.requests.Session.get", side_effect=fake_osrm_response
        ) as http_get:
            response = self.client.get(self.url, params)
        return response, http_get

    def test_plan_uses_single_routing_call_and_cheapest_stations(self):
        response, http_get = self._get(start="Denver, CO", finish="Indianapolis, IN")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()

        # City inputs resolve offline: the only outbound call is OSRM.
        self.assertEqual(http_get.call_count, 1)
        self.assertEqual(body["meta"]["external_api_call_names"], ["osrm"])

        ids = [s["opis_id"] for s in body["fuel_stops"]]
        self.assertNotIn(6, ids)  # outside the corridor
        self.assertIn(4, ids)  # cheapest reachable station
        summary = body["summary"]
        self.assertAlmostEqual(
            summary["fuel_cost_at_stops"], sum(s["cost"] for s in body["fuel_stops"]), places=1
        )
        # Station 1 is a few miles in; the fuel to get there is priced at its pump.
        assumed = summary["assumed_start_fuel"]
        self.assertEqual(assumed["priced_at"]["opis_id"], 1)
        self.assertAlmostEqual(
            summary["total_fuel_cost"], summary["fuel_cost_at_stops"] + assumed["cost"], places=1
        )
        self.assertAlmostEqual(summary["gallons_used"], 99.4, places=1)

        feature_kinds = {f["properties"]["kind"] for f in body["map"]["geojson"]["features"]}
        self.assertEqual(feature_kinds, {"route", "start", "finish", "fuel_stop"})
        self.assertIn("/map/?", body["map"]["html_url"])

    def test_repeat_request_is_served_from_cache_without_external_calls(self):
        self._get(start="Denver, CO", finish="Indianapolis, IN")
        response, http_get = self._get(start="Denver, CO", finish="Indianapolis, IN")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(http_get.call_count, 0)
        self.assertEqual(response.json()["meta"]["cache"], "hit")

    def test_post_with_full_tank(self):
        with mock.patch(
            "fuelplanner.services.http.requests.Session.get", side_effect=fake_osrm_response
        ):
            response = self.client.post(
                self.url,
                {"start": "39.74,-104.99", "finish": "39.77,-86.16", "start_fuel": 1},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["assumptions"]["start_fuel_gallons"], 50.0)

    def test_missing_finish_is_400(self):
        response, _ = self._get(start="Denver, CO")
        self.assertEqual(response.status_code, 400)
        self.assertIn("finish", response.json()["detail"])

    def test_non_us_coordinates_are_rejected(self):
        response, http_get = self._get(start="51.5,-0.12", finish="Denver, CO")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "location_outside_usa")
        self.assertEqual(http_get.call_count, 0)

    def test_canadian_city_is_rejected_without_geocoding(self):
        response, http_get = self._get(start="Toronto, ON", finish="Denver, CO")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "location_outside_usa")
        self.assertEqual(http_get.call_count, 0)

    def test_unknown_address_is_400_and_not_looked_up_twice(self):
        empty = mock.Mock(status_code=200)
        empty.json.return_value = []
        params = {"start": "123 Nowhere Lane, Atlantis", "finish": "Denver, CO"}
        with mock.patch(
            "fuelplanner.services.http.requests.Session.get", return_value=empty
        ) as http_get:
            first = self.client.get(self.url, params)
            second = self.client.get(self.url, params)
        self.assertEqual(first.status_code, 400)
        self.assertEqual(second.json()["error"], "location_not_found")
        self.assertEqual(http_get.call_count, 1)

    def test_data_gap_is_422_without_misleading_suggestions(self):
        FuelStation.objects.filter(opis_id__in=[3, 4, 5]).delete()
        reset_station_index()
        response, _ = self._get(start="Denver, CO", finish="Indianapolis, IN", corridor_miles=25)
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"], "no_feasible_fuel_plan")
        self.assertEqual(body["suggestions"], [])
        self.assertIn("no stations along that stretch", body["detail"])
        self.assertLess(body["gap"]["from_mile"], body["gap"]["to_mile"])

    def test_gap_closed_by_wider_corridor_suggests_it(self):
        FuelStation.objects.filter(opis_id__in=[3, 4, 5]).delete()
        FuelStation.objects.bulk_create([station(7, -94.5, 3.0, lat=40.2)])  # ~14 mi off route
        reset_station_index()
        response, _ = self._get(start="Denver, CO", finish="Indianapolis, IN")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["suggestions"], [{"corridor_miles": 25.0}])

    def test_html_map_renders(self):
        with mock.patch(
            "fuelplanner.services.http.requests.Session.get", side_effect=fake_osrm_response
        ):
            response = self.client.get(
                reverse("route-map"), {"start": "Denver, CO", "finish": "Indianapolis, IN"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "plan-geojson")
        self.assertContains(response, "Station 4")


class CorridorTests(TestCase):
    def test_route_mile_and_offset(self):
        FuelStation.objects.bulk_create(
            [
                station(1, -100.0, 3.0, lat=40.05),  # ~3.5 mi north of route
                station(2, -100.0, 3.0, lat=40.5),  # ~35 mi north: excluded
            ]
        )
        reset_station_index()
        found = stations_along_route(ROUTE_LAT, ROUTE_LON, 994.0, get_station_index(), 5.0)
        reset_station_index()
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].offset_miles, 3.45, delta=0.1)
        # -104 -> -100 at lat 40 is ~211 mi of the ~994 mi route.
        self.assertAlmostEqual(found[0].route_mile, 211.9 * 994.0 / 997.6, delta=2.0)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ExternalFailureTests(TestCase):
    url = "/api/route-plan/"

    def setUp(self):
        cache.clear()

    def _get(self, side_effect, **params):
        with mock.patch(
            "fuelplanner.services.http.requests.Session.get", side_effect=side_effect
        ) as http_get:
            return self.client.get(self.url, params), http_get

    def test_routing_outage_is_502(self):
        response, _ = self._get(
            requests.ConnectionError("boom"), start="Denver, CO", finish="Austin, TX"
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "routing_unavailable")

    def test_no_route_is_422(self):
        resp = mock.Mock(status_code=400)
        resp.json.return_value = {"code": "NoRoute", "message": "Impossible route"}
        response, _ = self._get([resp], start="Denver, CO", finish="Austin, TX")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "no_route")

    def test_street_address_is_geocoded_once_and_cached(self):
        nominatim = mock.Mock(status_code=200)
        nominatim.json.return_value = [
            {"lat": "39.7392", "lon": "-104.9903", "display_name": "Denver City Hall"}
        ]
        with (
            mock.patch("fuelplanner.services.geocoding.get_session") as session,
            mock.patch(
                "fuelplanner.services.planner.fetch_route", side_effect=NoRouteFound("stop here")
            ),
        ):
            session.return_value.get.return_value = nominatim
            for _ in range(2):
                response = self.client.get(
                    self.url, {"start": "1437 Bannock St, Denver", "finish": "Austin, TX"}
                )
                self.assertEqual(response.status_code, 422)
        self.assertEqual(session.return_value.get.call_count, 1)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ShortRouteTests(TestCase):
    """~370 mi route (Denver-ish to central Kansas), shorter than one tank."""

    def setUp(self):
        cache.clear()
        reset_station_index()

    def tearDown(self):
        reset_station_index()

    def _get(self, **params):
        lon = np.linspace(-104.0, -97.0, 200)
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 600_000.0,
                    "duration": 6 * 3600,
                    "geometry": {
                        "coordinates": np.column_stack((lon, np.full_like(lon, 40.0))).tolist()
                    },
                }
            ],
        }
        params = {"start": "Denver, CO", "finish": "Salina, KS", **params}
        with mock.patch("fuelplanner.services.http.requests.Session.get", return_value=resp):
            return self.client.get("/api/route-plan/", params)

    def test_no_stations_on_short_route_suggests_full_tank(self):
        response = self._get()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["suggestions"], [{"start_fuel": 1.0}])
        self.assertEqual(self._get(start_fuel=1).status_code, 200)

    def test_station_only_near_destination_is_not_a_free_trip(self):
        FuelStation.objects.bulk_create([station(1, -97.0, 3.5)])  # at the destination, like Reno
        reset_station_index()
        summary = self._get().json()["summary"]
        self.assertEqual(summary["number_of_stops"], 0)
        self.assertEqual(summary["fuel_cost_at_stops"], 0)
        # ~370 mi at 10 mpg priced at that station: about 37 gal * $3.50
        self.assertGreater(summary["total_fuel_cost"], 120)
        self.assertEqual(summary["total_fuel_cost"], summary["assumed_start_fuel"]["cost"])

    def test_map_page_explains_assumed_start_fuel(self):
        FuelStation.objects.bulk_create([station(1, -97.0, 3.5)])
        reset_station_index()
        lon = np.linspace(-104.0, -97.0, 200)
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 600_000.0,
                    "duration": 6 * 3600,
                    "geometry": {
                        "coordinates": np.column_stack((lon, np.full_like(lon, 40.0))).tolist()
                    },
                }
            ],
        }
        with mock.patch("fuelplanner.services.http.requests.Session.get", return_value=resp):
            page = self.client.get("/map/", {"start": "Denver, CO", "finish": "Salina, KS"})
        self.assertContains(page, "of start fuel, assumed to reach the first station")
        self.assertContains(page, "Station 1 (Town, KS")
        self.assertContains(page, "No stops needed")
