"""Cost-optimal refuelling along a fixed route (pure Python, no Django).

Model: a route of ``D`` miles; stations at mile markers ``m_i`` with price ``p_i``; tank
capacity ``C = max_range / mpg`` gallons; any amount may be bought at a stop; fuel use
is linear in distance. This is the classic single-route "gas station problem", for
which the following greedy is optimal (Khuller, Malekian & Mestre, 2007):

At the current station ``i`` holding ``f`` gallons, with range ``R`` on a full tank:

1. If a station ``j`` ahead within ``R`` is **cheaper** than ``i``, buy just enough to
   reach the nearest such ``j`` (possibly nothing) and go there.
2. Otherwise, if the destination is within ``R``, buy just enough to reach it. Done.
3. Otherwise **fill the tank** and go to the cheapest station within ``R``
   (tie-break: smaller off-route distance, then nearer).
4. If nothing is within ``R``, the trip is infeasible (:class:`UnreachableRouteError`).

Why it is optimal (exchange argument): fuel bought at ``i`` is only ever burned before
the next cheaper station, so buying more than needed to reach it would replace cheaper
fuel with dearer fuel; and when ``i`` is the cheapest station in reach, every gallon
burned in the next ``R`` miles is cheapest bought at ``i``, so filling up is optimal.

Start-fuel modes:

* ``full`` / numeric ``f0`` gallons — modelled by a free *virtual* station placed
  ``(C - f0) * mpg`` miles before the start: filling up there and driving to mile 0
  leaves exactly ``f0`` gallons, so the same greedy applies unchanged. Its "purchases"
  cost nothing and are not reported.
* ``empty`` — the vehicle has just enough fuel to reach its first stop, which must be
  within ``origin_radius_miles``. Every candidate first stop is tried; the fuel burned
  to reach it is charged at that stop's price, so the total is the cost of every gallon
  burned on the trip.

Complexity: ``O(n * k)`` for ``n`` stations and ``k`` stations within one tank range
(a simple forward scan; ``n`` is a few hundred, so this beats a monotonic-stack
precomputation in clarity at no measurable cost). ``empty`` mode repeats it for each
candidate first stop (usually a handful).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

EPS = 1e-9


class UnreachableRouteError(Exception):
    """No refuelling plan exists: a gap between fuel sources exceeds the vehicle's range."""

    def __init__(self, message: str, *, gap_start_mile: float, gap_end_mile: float) -> None:
        super().__init__(message)
        self.gap_start_mile = gap_start_mile
        self.gap_end_mile = gap_end_mile


@dataclass(frozen=True, slots=True)
class Vehicle:
    """Vehicle range and efficiency."""

    max_range_miles: float = 500.0
    mpg: float = 10.0

    @property
    def tank_gallons(self) -> float:
        """Tank capacity implied by range and efficiency."""
        return self.max_range_miles / self.mpg


class StartMode(StrEnum):
    EMPTY = "empty"
    FULL = "full"
    GALLONS = "gallons"


@dataclass(frozen=True, slots=True)
class StartFuel:
    """How much fuel the vehicle has at the start."""

    mode: StartMode
    gallons: float | None = None  # only for StartMode.GALLONS

    @classmethod
    def parse(cls, value: "str | float | StartFuel") -> "StartFuel":
        """``"empty"``, ``"full"`` or a positive number of gallons (``"12.5"`` or ``12.5``)."""
        if isinstance(value, StartFuel):
            return value
        if isinstance(value, str) and value.strip().lower() in ("empty", "full"):
            return cls(StartMode(value.strip().lower()))
        try:
            gallons = float(value)
        except (TypeError, ValueError):
            raise ValueError("start_fuel must be 'empty', 'full' or a number of gallons") from None
        if not gallons > 0 or gallons != gallons or gallons == float("inf"):
            raise ValueError("start_fuel gallons must be a positive finite number")
        return cls(StartMode.GALLONS, gallons)

    def initial_gallons(self, vehicle: Vehicle) -> float:
        """Gallons in the tank at mile 0 (0 for ``empty``)."""
        if self.mode is StartMode.FULL:
            return vehicle.tank_gallons
        if self.mode is StartMode.GALLONS:
            assert self.gallons is not None
            if self.gallons > vehicle.tank_gallons + EPS:
                raise ValueError(
                    f"start_fuel ({self.gallons:g} gal) exceeds the tank "
                    f"({vehicle.tank_gallons:g} gal)"
                )
            return self.gallons
        return 0.0

    def __str__(self) -> str:
        return f"{self.gallons:g}" if self.mode is StartMode.GALLONS else self.mode.value


@dataclass(frozen=True, slots=True)
class FuelCandidate:
    """A station as the optimizer sees it. ``key`` is the caller's identifier."""

    key: int
    mile: float
    price: float
    off_route_miles: float = 0.0


@dataclass(frozen=True, slots=True)
class FuelStop:
    """One stop of the plan."""

    candidate: FuelCandidate
    arrival_gallons: float
    purchased_gallons: float
    cost: float
    departure_gallons: float


@dataclass(frozen=True, slots=True)
class FuelPlan:
    """The optimal plan and its accounting (all values unrounded)."""

    stops: tuple[FuelStop, ...]
    total_miles: float
    start_fuel: StartFuel
    fuel_consumed_gallons: float
    gallons_purchased: float  # bought at stops
    pump_cost: float
    initial_fuel_gallons: float  # in the tank at the start (empty: burned to reach stop 1)
    initial_fuel_cost: float  # empty mode: charged at stop 1's price; otherwise 0
    fuel_at_finish_gallons: float

    @property
    def total_cost(self) -> float:
        """Money spent on fuel for this trip (see module docstring for the definition)."""
        return self.pump_cost + self.initial_fuel_cost

    @property
    def charged_gallons(self) -> float:
        """Gallons that were paid for (pump purchases + charged initial fuel)."""
        charged_initial = self.initial_fuel_gallons if self.initial_fuel_cost > 0 else 0.0
        return self.gallons_purchased + charged_initial

    @property
    def average_price_paid(self) -> float:
        """Total cost per paid gallon (0 if nothing was paid for)."""
        return self.total_cost / self.charged_gallons if self.charged_gallons > EPS else 0.0


# --- Preparation ----------------------------------------------------------------------


def prepare_candidates(
    candidates: Iterable[FuelCandidate], total_miles: float
) -> list[FuelCandidate]:
    """Sort by mile, keep stations strictly before the destination, and merge stations at
    the same mile marker (city-centroid duplicates) keeping the cheapest
    (then the closest to the road)."""
    best: dict[float, FuelCandidate] = {}
    for c in candidates:
        if c.mile < -EPS or c.mile >= total_miles - EPS:
            continue
        mile = round(max(c.mile, 0.0), 6)
        current = best.get(mile)
        if current is None or (c.price, c.off_route_miles) < (
            current.price,
            current.off_route_miles,
        ):
            best[mile] = FuelCandidate(c.key, mile, c.price, c.off_route_miles)
    return [best[m] for m in sorted(best)]


# --- Greedy core ----------------------------------------------------------------------


@dataclass(slots=True)
class _Node:
    mile: float
    price: float
    candidate: FuelCandidate | None  # None for the virtual start station


def _greedy(
    stations: Sequence[FuelCandidate],
    start: _Node,
    start_index: int,
    fuel: float,
    total_miles: float,
    vehicle: Vehicle,
) -> tuple[list[FuelStop], float]:
    """Run the greedy from ``start`` (holding ``fuel``). ``stations[start_index:]`` are the
    stations strictly ahead. Returns the real stops and the fuel left at the destination.
    """
    capacity, mpg, reach = vehicle.tank_gallons, vehicle.mpg, vehicle.max_range_miles
    stops: list[FuelStop] = []
    node, i = start, start_index  # stations[i:] are ahead of `node`

    def buy(amount: float) -> None:
        nonlocal fuel
        amount = max(0.0, amount)
        if node.candidate is not None and amount > EPS:
            stops.append(FuelStop(node.candidate, fuel, amount, amount * node.price, fuel + amount))
        fuel += amount

    while True:
        limit = node.mile + reach + EPS
        in_range: list[int] = []
        j = i
        while j < len(stations) and stations[j].mile <= limit:
            in_range.append(j)
            j += 1

        cheaper = next((k for k in in_range if stations[k].price < node.price - EPS), None)
        if cheaper is not None:  # rule 1: just enough to reach the next cheaper station
            target = stations[cheaper]
            buy((target.mile - node.mile) / mpg - fuel)
        elif total_miles - node.mile <= reach + EPS:  # rule 2: just enough to finish
            buy((total_miles - node.mile) / mpg - fuel)
            fuel -= (total_miles - node.mile) / mpg
            return stops, max(fuel, 0.0)
        elif in_range:  # rule 3: fill up, go to the cheapest station in range
            buy(capacity - fuel)
            cheaper = min(
                in_range,
                key=lambda k: (stations[k].price, stations[k].off_route_miles, stations[k].mile),
            )
            target = stations[cheaper]
        else:  # rule 4: nothing reachable
            next_mile = stations[i].mile if i < len(stations) else total_miles
            raise UnreachableRouteError(
                f"No fuel available between mile {max(node.mile, 0.0):.1f} and mile "
                f"{next_mile:.1f} ({next_mile - max(node.mile, 0.0):.1f} miles > range "
                f"{reach:g} miles).",
                gap_start_mile=max(node.mile, 0.0),
                gap_end_mile=next_mile,
            )

        fuel = max(fuel - (target.mile - node.mile) / mpg, 0.0)  # clamp float dust
        node, i = _Node(target.mile, target.price, target), cheaper + 1


# --- Public entry point -----------------------------------------------------------------


def plan_fuel_stops(
    candidates: Iterable[FuelCandidate],
    total_miles: float,
    vehicle: Vehicle,
    start_fuel: "StartFuel | str | float" = "empty",
    origin_radius_miles: float = 25.0,
) -> FuelPlan:
    """Compute the minimum-cost refuelling plan for a route of ``total_miles``.

    Raises :class:`UnreachableRouteError` if no feasible plan exists and ``ValueError``
    for invalid inputs (e.g. start fuel larger than the tank).
    """
    if total_miles < 0 or vehicle.mpg <= 0 or vehicle.max_range_miles <= 0:
        raise ValueError("total_miles must be >= 0; range and mpg must be positive")
    start = StartFuel.parse(start_fuel)
    stations = prepare_candidates(candidates, total_miles)
    consumed = total_miles / vehicle.mpg

    if start.mode is StartMode.EMPTY:
        return _plan_from_empty(stations, total_miles, vehicle, start, origin_radius_miles)

    initial = start.initial_gallons(vehicle)
    virtual_mile = -(vehicle.tank_gallons - initial) * vehicle.mpg
    # The virtual station starts with a full tank, so at mile 0 exactly `initial` is left.
    stops, left = _greedy(
        stations, _Node(virtual_mile, 0.0, None), 0, vehicle.tank_gallons, total_miles, vehicle
    )
    purchased = sum(s.purchased_gallons for s in stops)
    return FuelPlan(
        stops=tuple(stops),
        total_miles=total_miles,
        start_fuel=start,
        fuel_consumed_gallons=consumed,
        gallons_purchased=purchased,
        pump_cost=sum(s.cost for s in stops),
        initial_fuel_gallons=initial,
        initial_fuel_cost=0.0,
        fuel_at_finish_gallons=left,
    )


def _plan_from_empty(
    stations: list[FuelCandidate],
    total_miles: float,
    vehicle: Vehicle,
    start: StartFuel,
    origin_radius_miles: float,
) -> FuelPlan:
    window = [k for k, s in enumerate(stations) if s.mile <= origin_radius_miles + EPS]
    if not window:
        nearest = stations[0].mile if stations else total_miles
        raise UnreachableRouteError(
            f"start_fuel=empty needs a station within {origin_radius_miles:g} miles of the "
            f"start; the nearest on this route is at mile {nearest:.1f}.",
            gap_start_mile=0.0,
            gap_end_mile=nearest,
        )

    best: FuelPlan | None = None
    last_error: UnreachableRouteError | None = None
    for k in window:
        first = stations[k]
        try:
            stops, left = _greedy(
                stations, _Node(first.mile, first.price, first), k + 1, 0.0, total_miles, vehicle
            )
        except UnreachableRouteError as exc:
            last_error = exc
            continue
        initial = first.mile / vehicle.mpg
        plan = FuelPlan(
            stops=tuple(stops),
            total_miles=total_miles,
            start_fuel=start,
            fuel_consumed_gallons=total_miles / vehicle.mpg,
            gallons_purchased=sum(s.purchased_gallons for s in stops),
            pump_cost=sum(s.cost for s in stops),
            initial_fuel_gallons=initial,
            initial_fuel_cost=initial * first.price,
            fuel_at_finish_gallons=left,
        )
        if best is None or plan.total_cost < best.total_cost - EPS:
            best = plan
    if best is None:
        assert last_error is not None
        raise last_error
    return best
