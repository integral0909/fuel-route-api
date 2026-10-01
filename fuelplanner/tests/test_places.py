from django.test import SimpleTestCase

from fuelplanner.services.places import census_name_variants, lookup_city_state, normalize_place


class PlaceNormalizationTests(SimpleTestCase):
    def test_saint_and_mount_abbreviations_match(self):
        self.assertEqual(normalize_place("St. Louis"), normalize_place("Saint Louis"))
        self.assertEqual(normalize_place("Mt Vernon"), normalize_place("Mount Vernon"))
        self.assertEqual(normalize_place("Mc Calla"), normalize_place("McCalla"))

    def test_census_suffixes_are_stripped(self):
        self.assertIn("Big Cabin", census_name_variants("Big Cabin town"))
        self.assertIn("Boise", census_name_variants("Boise City city"))
        self.assertIn(
            "Athens", census_name_variants("Athens-Clarke County unified government (balance)")
        )


class OfflineGazetteerTests(SimpleTestCase):
    def test_city_state_inputs_resolve_without_network(self):
        for query in ("Chicago, IL", "chicago, illinois", "Los Angeles, CA, USA", "St. Louis, MO"):
            with self.subTest(query=query):
                place = lookup_city_state(query)
                self.assertIsNotNone(place)

        chicago = lookup_city_state("Chicago, IL")
        self.assertAlmostEqual(chicago.lat, 41.84, delta=0.2)
        self.assertAlmostEqual(chicago.lon, -87.68, delta=0.2)

    def test_unknown_or_non_us_inputs_return_none(self):
        self.assertIsNone(lookup_city_state("Toronto, ON"))
        self.assertIsNone(lookup_city_state("1600 Pennsylvania Ave NW Washington"))
