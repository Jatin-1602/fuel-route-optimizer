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

Stop penalty (``stop_penalty > 0``): minimise ``fuel cost + penalty x number of stops``.
The greedy is no longer optimal (it happily stops for 1 gallon to save 3 cents), so an
exact DP is used instead (:func:`_penalized_stop_set`). It relies on the known structure
of optimal plans: at every stop you either **fill up** or buy **just enough to arrive
empty** at the next stop (or at the destination). Hence the fuel on arrival at a station
is 0, ``C - d(j, l)`` for a previous station ``j`` that filled up, or the start fuel
minus the distance driven — finitely many states. The DP returns the optimal *set* of
stops; the greedy restricted to that set then computes the purchases (the greedy is
optimal for any fixed set of stations, so the objective cannot get worse).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

import numpy as np

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
    stop_penalty: float = 0.0  # USD per stop used when choosing the plan

    @property
    def objective(self) -> float:
        """What the plan minimises: fuel cost + stop_penalty x number of stops."""
        return self.total_cost + self.stop_penalty * len(self.stops)

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
    stop_penalty: float = 0.0,
) -> FuelPlan:
    """Compute the refuelling plan minimising fuel cost + ``stop_penalty`` x stops.

    ``stop_penalty == 0`` runs the greedy (pure cost optimum); a positive penalty runs
    the exact DP. Raises :class:`UnreachableRouteError` if no feasible plan exists and
    ``ValueError`` for invalid inputs (e.g. start fuel larger than the tank).
    """
    if total_miles < 0 or vehicle.mpg <= 0 or vehicle.max_range_miles <= 0:
        raise ValueError("total_miles must be >= 0; range and mpg must be positive")
    if not 0 <= stop_penalty < float("inf"):
        raise ValueError("stop_penalty must be a non-negative finite number")
    start = StartFuel.parse(start_fuel)
    start.initial_gallons(vehicle)  # validates start fuel against the tank
    stations = prepare_candidates(candidates, total_miles)

    if stop_penalty > 0:
        chosen = _penalized_stop_set(
            stations, total_miles, vehicle, start, origin_radius_miles, stop_penalty
        )
        if chosen is not None:
            stations = [stations[k] for k in chosen]
        # chosen is None -> infeasible; the greedy below raises with the gap details.
    plan = _plan_greedy(stations, total_miles, vehicle, start, origin_radius_miles)
    return replace(plan, stop_penalty=stop_penalty)


def _plan_greedy(
    stations: list[FuelCandidate],
    total_miles: float,
    vehicle: Vehicle,
    start: StartFuel,
    origin_radius_miles: float,
) -> FuelPlan:
    """Pure cost optimum over ``stations`` (already prepared)."""
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


# --- Stop-penalty DP ---------------------------------------------------------------------


def _penalized_stop_set(
    stations: list[FuelCandidate],
    total_miles: float,
    vehicle: Vehicle,
    start: StartFuel,
    origin_radius_miles: float,
    penalty: float,
) -> list[int] | None:
    """Indices of the stops minimising fuel cost + ``penalty`` x stops (``None`` if infeasible).

    Forward DP over stations in mile order. The states at station ``l`` (fuel on arrival
    ``a``, cost so far ``c``) are:

    * start states — start fuel minus distance driven (cost 0), or for ``empty`` mode an
      empty tank at a station within the origin radius (cost: fuel to get there, charged
      at its price);
    * ``(C - d(j, l), F_j)`` for every earlier ``j`` within range that **filled up**, where
      ``F_j = min_states_at_j(c - a p_j) + C p_j + penalty`` (one number per station);
    * ``(0, E_l)`` — arrived empty because the previous stop bought just enough, where
      ``E_l = min_j [min_{a <= d(j,l)} (c - a p_j) + d(j,l) p_j + penalty]``; the inner min
      is a prefix minimum over ``j``'s states sorted by ``a``, vectorised over all ``l``.

    Each station is processed with a handful of numpy operations: ``O(n k)`` overall.
    """
    n = len(stations)
    if n == 0:
        return [] if _finishes_without_stops(total_miles, vehicle, start) else None
    cap, mpg, reach = vehicle.tank_gallons, vehicle.mpg, vehicle.max_range_miles
    miles = np.fromiter((s.mile for s in stations), dtype=np.float64, count=n)
    prices = np.fromiter((s.price for s in stations), dtype=np.float64, count=n)

    # Start states.
    if start.mode is StartMode.EMPTY:
        init_ok = miles <= origin_radius_miles + EPS
        init_a = np.zeros(n)
        init_c = miles / mpg * prices
    else:
        f0 = start.initial_gallons(vehicle)
        init_ok = miles <= f0 * mpg + EPS
        init_a = f0 - miles / mpg
        init_c = np.zeros(n)

    fill_cost = np.full(n, np.inf)  # F_j
    fill_from = np.full(n, -1, dtype=np.int64)  # local state index at j achieving F_j
    empty_cost = np.full(n, np.inf)  # E_l
    empty_last = np.full(n, -1, dtype=np.int64)  # station that bought just enough
    empty_prev = np.full(n, -1, dtype=np.int64)  # its local state index
    # Per station: (last stop, its local state index) for every state, for reconstruction.
    back: list[tuple[np.ndarray, np.ndarray] | None] = [None] * n

    no_stop = _finishes_without_stops(total_miles, vehicle, start)
    best_cost, best_station, best_state = (0.0 if no_stop else np.inf), -1, -1

    for cur in range(n):
        lo = int(np.searchsorted(miles, miles[cur] - reach - EPS, side="left"))
        fillers = np.arange(lo, cur)[np.isfinite(fill_cost[lo:cur])]
        a = [cap - (miles[cur] - miles[fillers]) / mpg]
        c = [fill_cost[fillers]]
        last = [fillers]
        prev = [fill_from[fillers]]
        if np.isfinite(empty_cost[cur]):
            a.append(np.zeros(1))
            c.append(empty_cost[cur : cur + 1])
            last.append(empty_last[cur : cur + 1])
            prev.append(empty_prev[cur : cur + 1])
        if init_ok[cur]:
            a.append(init_a[cur : cur + 1])
            c.append(init_c[cur : cur + 1])
            last.append(np.full(1, -1, dtype=np.int64))
            prev.append(np.full(1, -1, dtype=np.int64))
        a_l, c_l = np.concatenate(a), np.concatenate(c)
        if a_l.size == 0:
            continue
        back[cur] = (np.concatenate(last), np.concatenate(prev))
        price = prices[cur]
        key = c_l - a_l * price  # cost of the state if its fuel were bought here

        # Finish: buy just enough to reach the destination.
        need = (total_miles - miles[cur]) / mpg
        if need <= cap + EPS:
            short = np.flatnonzero(a_l < need - EPS)
            if short.size:
                totals = key[short] + need * price + penalty
                i = int(np.argmin(totals))
                if totals[i] < best_cost - EPS:
                    best_cost, best_station, best_state = float(totals[i]), cur, int(short[i])

        # Fill up here.
        i = int(np.argmin(key))
        fill_cost[cur] = key[i] + cap * price + penalty
        fill_from[cur] = i

        # Buy just enough to arrive empty at each station ahead within range.
        hi = int(np.searchsorted(miles, miles[cur] + reach + EPS, side="right"))
        if hi <= cur + 1:
            continue
        ahead = np.arange(cur + 1, hi)
        dist = (miles[ahead] - miles[cur]) / mpg
        order = np.argsort(a_l, kind="stable")
        a_sorted, key_sorted = a_l[order], key[order]
        prefix_min = np.minimum.accumulate(key_sorted)
        prefix_arg = np.maximum.accumulate(
            np.where(key_sorted <= prefix_min, np.arange(key_sorted.size), 0)
        )
        pos = np.searchsorted(a_sorted, dist + EPS, side="right") - 1
        valid = pos >= 0
        targets, pos = ahead[valid], pos[valid]
        costs = prefix_min[pos] + dist[valid] * price + penalty
        better = costs < empty_cost[targets] - EPS
        targets, pos = targets[better], pos[better]
        empty_cost[targets] = costs[better]
        empty_last[targets] = cur
        empty_prev[targets] = order[prefix_arg[pos]]

    if not np.isfinite(best_cost):
        return None
    chosen: list[int] = []
    station, state = best_station, best_state
    while station != -1:
        chosen.append(station)
        backrefs = back[station]
        assert backrefs is not None
        station, state = int(backrefs[0][state]), int(backrefs[1][state])
    return sorted(chosen)


def _finishes_without_stops(total_miles: float, vehicle: Vehicle, start: StartFuel) -> bool:
    return (
        start.mode is not StartMode.EMPTY
        and total_miles <= start.initial_gallons(vehicle) * vehicle.mpg + EPS
    )
