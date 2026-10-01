from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from fuelplanner.models import FuelStation
from fuelplanner.services.station_index import reset_station_index
from fuelplanner.throttling import RoutePlanThrottle

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class HealthCheckTests(TestCase):
    def test_unhealthy_until_stations_are_loaded(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 503)

        call_command("load_stations", stdout=StringIO())
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()["stations"], 6000)

    def tearDown(self):
        reset_station_index()


class LoadStationsTests(TestCase):
    def tearDown(self):
        reset_station_index()

    def test_if_empty_does_not_reload(self):
        call_command("load_stations", stdout=StringIO())
        FuelStation.objects.filter(state="TX").delete()
        remaining = FuelStation.objects.count()

        out = StringIO()
        call_command("load_stations", "--if-empty", stdout=out)
        self.assertIn("skipping", out.getvalue())
        self.assertEqual(FuelStation.objects.count(), remaining)


@override_settings(CACHES=LOCMEM)
class ThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(RoutePlanThrottle, "THROTTLE_RATES", {"route_plan": "2/min"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_api_is_rate_limited(self):
        url = reverse("route-plan")
        codes = [self.client.get(url).status_code for _ in range(3)]
        self.assertEqual(codes, [400, 400, 429])  # invalid requests still count

    def test_map_page_shares_the_limit(self):
        self.client.get(reverse("route-plan"))
        self.client.get(reverse("route-plan"))
        response = self.client.get(reverse("route-map"), {"start": "Denver, CO"})
        self.assertEqual(response.status_code, 429)

    def test_empty_map_page_is_not_throttled(self):
        for _ in range(3):
            self.assertEqual(self.client.get(reverse("route-map")).status_code, 200)
