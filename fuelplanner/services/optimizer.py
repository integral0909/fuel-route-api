# Min-cost refuelling on a fixed route, a.k.a. the gas station problem.
#
# Fuel is tracked in miles of range. We minimise
#     fuel cost + stop_penalty * stops
# (penalty 0 = cheapest possible plan, but that plan is full of 0.2 gal
# top-ups, hence the penalty).
#
# Khuller, Malekian & Mestre, "To fill or not to fill" (2007): for a fixed set
# of stops there's an optimal plan where every stop either fills the tank or
# buys just enough to reach the next stop. So fuel on arriving at stop w can
# only be 0, C - d(v, w) for an earlier stop v, or start_fuel - d(0, w), which
# keeps the DP small. tests/test_optimizer.py checks it against an LP solver.
from bisect import bisect_right
from dataclasses import dataclass, field

EPS = 1e-9
FILL, JUST_ENOUGH, START = "fill", "just_enough", "start"


class InfeasibleRoute(Exception):
    def __init__(self, gap_start, gap_end, tank_range, message=None):
        self.gap_start_mile = gap_start
        self.gap_end_mile = gap_end
        super().__init__(
            message
            or f"No fuel station within {tank_range:.0f} miles between route mile "
            f"{gap_start:.1f} and {gap_end:.1f}."
        )


@dataclass(frozen=True)
class Stop:
    key: object
    mile: float
    price: float  # $/gal


@dataclass(frozen=True)
class Purchase:
    stop: Stop
    gallons: float
    cost: float
    fuel_on_arrival_gallons: float


@dataclass
class FuelPlan:
    purchases: list = field(default_factory=list)
    start_fuel_miles: float = 0.0
    arrival_fuel_gallons: float = 0.0
    # Range we had to assume beyond the requested start fuel, priced at the first
    # station on the route (the one the driver would otherwise have filled at).
    assumed_fuel_miles: float = 0.0
    assumed_fuel_stop: Stop | None = None
    mpg: float = 1.0

    @property
    def total_cost(self):
        """Spend at the recommended stops only."""
        return sum(p.cost for p in self.purchases)

    @property
    def assumed_fuel_gallons(self):
        return self.assumed_fuel_miles / self.mpg

    @property
    def assumed_fuel_cost(self):
        if not self.assumed_fuel_stop:
            return 0.0
        return self.assumed_fuel_gallons * self.assumed_fuel_stop.price

    @property
    def trip_cost(self):
        return self.total_cost + self.assumed_fuel_cost

    @property
    def total_gallons(self):
        return sum(p.gallons for p in self.purchases)


@dataclass
class _State:
    fuel: float  # miles of range on arrival
    cost: float
    parent: "_State" = None
    node: int = -1
    action: str = ""  # what was done at the parent stop


def plan_fuel_stops(stops, total_miles, tank_range, mpg, start_fuel_miles, stop_penalty=0.0):
    stops = sorted((s for s in stops if -EPS <= s.mile <= total_miles + EPS), key=lambda s: s.mile)
    cap = tank_range

    # Leaving with less fuel than it takes to reach the first station can't
    # work, so assume just enough to get there. That fuel isn't free: it's
    # priced at the first station (see FuelPlan.assumed_fuel_cost).
    requested = min(max(start_fuel_miles, 0.0), cap)
    start_fuel = requested
    if total_miles > start_fuel + EPS:
        if not stops or stops[0].mile > cap + EPS:
            raise InfeasibleRoute(
                0.0,
                stops[0].mile if stops else total_miles,
                cap,
                _unreachable_start_message(stops, total_miles, cap, requested),
            )
        start_fuel = max(start_fuel, stops[0].mile)

    marks = [s.mile for s in stops] + [total_miles]
    for a, b in zip(marks, marks[1:], strict=False):
        if b - a > cap + EPS:
            raise InfeasibleRoute(a, b, cap)

    n = len(stops)
    dest = n
    states = [[] for _ in range(n + 1)]
    arrive_empty = [None] * (n + 1)  # best "arrive with 0" state per node

    origin = _State(start_fuel, 0.0)
    for w in range(n + 1):
        if marks[w] > start_fuel + EPS:
            break
        states[w].append(_State(start_fuel - marks[w], 0.0, origin, w, START))

    for v in range(n):
        if arrive_empty[v]:
            states[v].append(arrive_empty[v])
        here = states[v]
        if not here:
            continue
        per_mile = stops[v].price / mpg

        # Filling up: the best state to fill from doesn't depend on where we go next.
        fill_from = min(here, key=lambda s: s.cost + (cap - s.fuel) * per_mile)
        fill_cost = fill_from.cost + (cap - fill_from.fuel) * per_mile + stop_penalty

        # Just enough to reach w only works if we arrived with <= d(v, w), so
        # sort by fuel and keep a running best of (cost - fuel * price).
        by_fuel = sorted(here, key=lambda s: s.fuel)
        fuels = [s.fuel for s in by_fuel]
        running_best, best = [], None
        for s in by_fuel:
            if best is None or s.cost - s.fuel * per_mile < best.cost - best.fuel * per_mile:
                best = s
            running_best.append(best)

        w = v + 1
        while w <= n and marks[w] - marks[v] <= cap + EPS:
            d = marks[w] - marks[v]
            if w != dest:
                states[w].append(_State(cap - d, fill_cost, fill_from, w, FILL))
            i = bisect_right(fuels, d + EPS) - 1
            if i >= 0:
                prev = running_best[i]
                buy = d - prev.fuel
                if buy > EPS:
                    cost = prev.cost + buy * per_mile + stop_penalty
                    if arrive_empty[w] is None or cost < arrive_empty[w].cost - EPS:
                        arrive_empty[w] = _State(0.0, cost, prev, w, JUST_ENOUGH)
            w += 1

    finals = [s for s in states[dest] + [arrive_empty[dest]] if s is not None]
    if not finals:
        # shouldn't happen after the gap check above
        raise InfeasibleRoute(0.0, total_miles, cap)
    best_final = min(finals, key=lambda s: s.cost)
    plan = _build_plan(best_final, stops, marks, cap, mpg, start_fuel)
    if start_fuel > requested + EPS:
        plan.assumed_fuel_miles = start_fuel - requested
        plan.assumed_fuel_stop = stops[0]
    return plan


def _build_plan(final, stops, marks, cap, mpg, start_fuel):
    chain = []
    s = final
    while s.parent is not None and s.action != START:
        chain.append(s)
        s = s.parent

    plan = FuelPlan(start_fuel_miles=start_fuel, arrival_fuel_gallons=final.fuel / mpg, mpg=mpg)
    for arrival in reversed(chain):
        at = arrival.parent
        stop = stops[at.node]
        if arrival.action == FILL:
            buy = cap - at.fuel
        else:
            buy = marks[arrival.node] - marks[at.node] - at.fuel
        if buy <= EPS:
            continue
        gallons = buy / mpg
        plan.purchases.append(Purchase(stop, gallons, gallons * stop.price, at.fuel / mpg))
    return plan


def _unreachable_start_message(stops, total_miles, cap, start_fuel):
    tank = "starts empty" if start_fuel <= EPS else f"starts with {start_fuel:.0f} miles of range"
    if not stops:
        return (
            f"There's no fuel station on this route, and the tank {tank} "
            f"for a {total_miles:.1f}-mile trip."
        )
    return (
        f"The first fuel station on the route is at mile {stops[0].mile:.1f}, "
        f"beyond the {cap:.0f}-mile range from the start."
    )
