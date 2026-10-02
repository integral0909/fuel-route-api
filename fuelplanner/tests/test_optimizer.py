import random

import numpy as np
from django.test import SimpleTestCase
from scipy.optimize import linprog

from fuelplanner.services.optimizer import InfeasibleRoute, Stop, plan_fuel_stops

RANGE, MPG = 500.0, 10.0


def plan(stops, total, start_fuel=0.0, stop_penalty=0.0):
    return plan_fuel_stops(
        [Stop(key=f"s{i}", mile=m, price=p) for i, (m, p) in enumerate(stops)],
        total_miles=total,
        tank_range=RANGE,
        mpg=MPG,
        start_fuel_miles=start_fuel,
        stop_penalty=stop_penalty,
    )


def lp_optimum(stops, total, start_fuel):
    """Exact optimum via linear programming (fuel in miles, x_i bought at stop i)."""
    miles = np.array([m for m, _ in stops])
    prices = np.array([p for _, p in stops])
    n = len(stops)
    A, b = [], []
    for k in range(n):
        # fuel on arrival at k >= 0:  start + sum_{i<k} x_i - miles[k] >= 0
        A.append([-1.0 if i < k else 0.0 for i in range(n)])
        b.append(start_fuel - miles[k])
        # tank after buying at k <= RANGE:  start + sum_{i<=k} x_i - miles[k] <= RANGE
        A.append([1.0 if i <= k else 0.0 for i in range(n)])
        b.append(RANGE - start_fuel + miles[k])
    A.append([-1.0] * n)
    b.append(start_fuel - total)  # reach destination
    res = linprog(prices / MPG, A_ub=A, b_ub=b, bounds=[(0, None)] * n, method="highs")
    return res.fun if res.status == 0 else None


class OptimizerTests(SimpleTestCase):
    def test_short_trip_with_full_tank_needs_no_fuel(self):
        result = plan([(100, 3.0)], total=400, start_fuel=RANGE)
        self.assertEqual(result.purchases, [])
        self.assertAlmostEqual(result.arrival_fuel_gallons, 10.0)

    def test_buys_only_enough_to_reach_cheaper_station(self):
        # Start empty at an expensive station, a cheap one 100 mi later.
        result = plan([(0, 4.0), (100, 3.0)], total=550)
        first, second = result.purchases
        self.assertAlmostEqual(first.gallons, 10.0)  # 100 miles worth
        self.assertAlmostEqual(second.gallons, 45.0)  # remaining 450 miles
        self.assertAlmostEqual(result.total_cost, 10 * 4.0 + 45 * 3.0)
        self.assertAlmostEqual(result.arrival_fuel_gallons, 0.0)

    def test_fills_up_at_cheap_station_before_expensive_stretch(self):
        result = plan([(0, 3.0), (300, 5.0), (600, 4.0)], total=900)
        # Fill at mile 0; buy only the 100 miles needed at the $5 station to
        # reach the $4 one (mile 600 is out of range from mile 0).
        self.assertEqual([p.stop.mile for p in result.purchases], [0, 300, 600])
        self.assertEqual([round(p.gallons, 6) for p in result.purchases], [50.0, 10.0, 30.0])

    def test_arrives_with_minimal_fuel(self):
        result = plan([(0, 3.0)], total=200)
        self.assertAlmostEqual(result.total_gallons, 20.0)
        self.assertAlmostEqual(result.arrival_fuel_gallons, 0.0)

    def test_gap_longer_than_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRoute) as ctx:
            plan([(0, 3.0), (600, 3.0)], total=700)
        self.assertAlmostEqual(ctx.exception.gap_start_mile, 0.0)
        self.assertAlmostEqual(ctx.exception.gap_end_mile, 600.0)

    def test_empty_start_is_raised_to_reach_first_station(self):
        result = plan([(12, 3.0)], total=300, start_fuel=0.0)
        self.assertAlmostEqual(result.start_fuel_miles, 12.0)
        self.assertAlmostEqual(result.total_gallons, 28.8)

    def test_assumed_start_fuel_is_priced_at_first_station(self):
        # Only station is near the end: the assumed fuel is most of the trip.
        result = plan([(480, 3.0)], total=490, start_fuel=0.0)
        self.assertAlmostEqual(result.assumed_fuel_gallons, 48.0)
        self.assertAlmostEqual(result.assumed_fuel_cost, 144.0)
        self.assertAlmostEqual(result.total_cost, 3.0)  # 1 gal bought at the stop
        self.assertAlmostEqual(result.trip_cost, 147.0)

    def test_declared_start_fuel_is_not_priced(self):
        result = plan([(100, 3.0)], total=400, start_fuel=RANGE)
        self.assertIsNone(result.assumed_fuel_stop)
        self.assertEqual(result.trip_cost, 0.0)

    def test_stop_penalty_skips_micro_top_ups(self):
        # A station a hair cheaper 20 miles on: the pure optimum stops twice.
        stops = [(0, 3.10), (20, 3.09)]
        self.assertEqual(len(plan(stops, 400).purchases), 2)
        penalised = plan(stops, 400, stop_penalty=5.0)
        self.assertEqual(len(penalised.purchases), 1)
        self.assertAlmostEqual(penalised.total_gallons, 40.0)

    def test_stop_penalty_never_increases_stops(self):
        rng = random.Random(7)
        for _ in range(200):
            total = rng.uniform(300, 2500)
            stops = sorted(
                (rng.uniform(0, total), round(rng.uniform(2.8, 4.6), 3))
                for _ in range(rng.randint(5, 40))
            )
            try:
                cheapest = plan(stops, total)
            except InfeasibleRoute:
                continue
            fewer = plan(stops, total, stop_penalty=10.0)
            self.assertLessEqual(len(fewer.purchases), len(cheapest.purchases))
            self.assertGreaterEqual(fewer.total_cost, cheapest.total_cost - 1e-6)
            self.assertAlmostEqual(
                fewer.total_gallons - fewer.arrival_fuel_gallons,
                cheapest.total_gallons - cheapest.arrival_fuel_gallons,
                places=6,
            )

    def test_matches_linear_programming_optimum(self):
        rng = random.Random(42)
        checked = 0
        for _ in range(300):
            total = rng.uniform(200, 2500)
            n = rng.randint(1, 25)
            stops = sorted(
                (rng.uniform(0, total), round(rng.uniform(2.8, 4.6), 3)) for _ in range(n)
            )
            start_fuel = rng.choice([0.0, rng.uniform(0, RANGE), RANGE])
            try:
                greedy = plan(stops, total, start_fuel)
            except InfeasibleRoute:
                continue
            optimum = lp_optimum(stops, total, greedy.start_fuel_miles)
            self.assertIsNotNone(optimum)
            self.assertAlmostEqual(greedy.total_cost, optimum, places=6)
            checked += 1
        self.assertGreater(checked, 50)
