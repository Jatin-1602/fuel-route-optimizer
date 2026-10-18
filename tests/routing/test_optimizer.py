"""Fuel optimizer: worked examples, edge cases, and a brute-force optimality check."""

import math
import random

import numpy as np
import pytest

from apps.routing.services.optimizer import (
    FuelCandidate,
    StartFuel,
    StartMode,
    UnreachableRouteError,
    Vehicle,
    plan_fuel_stops,
    prepare_candidates,
)

SMALL = Vehicle(max_range_miles=100, mpg=10)  # 10-gallon tank
TRUCK = Vehicle()  # 500 miles, 10 mpg, 50-gallon tank


def cands(*stations: tuple[float, float]) -> list[FuelCandidate]:
    """``(mile, price)`` pairs -> candidates keyed by position."""
    return [FuelCandidate(k, m, p) for k, (m, p) in enumerate(stations)]


def stops_of(plan) -> list[tuple[int, float, float]]:
    """``(key, gallons, cost)`` per stop, rounded for readable assertions."""
    return [(s.candidate.key, round(s.purchased_gallons, 6), round(s.cost, 6)) for s in plan.stops]


# --- The worked example from docs/ALGORITHM.md ---------------------------------------------

WORKED = cands((0, 3.00), (60, 3.50), (90, 2.80), (170, 3.20), (200, 3.60))  # A B C D E


def test_worked_example_step_by_step() -> None:
    plan = plan_fuel_stops(WORKED, 250, SMALL, "empty", origin_radius_miles=25)

    # A: C ($2.80) is the nearest cheaper station in range -> buy just enough for 90 mi (B skipped)
    # C: nothing cheaper in range, finish 160 mi away -> fill 10 gal, go to cheapest in range (D)
    # D: arrive with 2 gal, finish within range -> buy just the 6 gal needed
    assert stops_of(plan) == [(0, 9.0, 27.0), (2, 10.0, 28.0), (3, 6.0, 19.2)]
    assert plan.total_cost == pytest.approx(74.2)
    assert plan.gallons_purchased == pytest.approx(25.0) == plan.fuel_consumed_gallons
    assert plan.fuel_at_finish_gallons == pytest.approx(0.0)
    assert [round(s.arrival_gallons, 6) for s in plan.stops] == [0.0, 0.0, 2.0]


def test_worked_example_matches_brute_force() -> None:
    assert brute_force(WORKED, 250, SMALL, StartFuel(StartMode.EMPTY), 25) == pytest.approx(74.2)


# --- Edge cases (PROMPT §3.10) --------------------------------------------------------------


def test_short_trip_full_tank_needs_no_stop() -> None:
    plan = plan_fuel_stops(cands((10, 3.0)), 80, SMALL, "full")
    assert plan.stops == ()
    assert plan.total_cost == 0.0
    assert plan.fuel_at_finish_gallons == pytest.approx(10 - 8)


def test_short_trip_empty_needs_exactly_one_stop() -> None:
    plan = plan_fuel_stops(cands((5, 3.0), (20, 2.0)), 80, SMALL, "empty", origin_radius_miles=25)
    # Start at the $2.00 station 20 miles out: 2 gal burned to get there are charged at $2.00.
    assert stops_of(plan) == [(1, 6.0, 12.0)]
    assert plan.initial_fuel_gallons == pytest.approx(2.0)
    assert plan.initial_fuel_cost == pytest.approx(4.0)
    assert plan.total_cost == pytest.approx(16.0)  # 8 gal * $2.00: every gallon burned


def test_destination_exactly_at_range_limit() -> None:
    plan = plan_fuel_stops(cands((50, 3.0)), 100, SMALL, "full")
    assert plan.stops == ()
    assert plan.fuel_at_finish_gallons == pytest.approx(0.0)


def test_one_mile_beyond_range_needs_a_stop() -> None:
    plan = plan_fuel_stops(cands((50, 3.0)), 101, SMALL, "full")
    assert stops_of(plan) == [(0, pytest.approx(0.1), pytest.approx(0.3))]


def test_gap_larger_than_range_is_unreachable() -> None:
    with pytest.raises(UnreachableRouteError) as exc_info:
        plan_fuel_stops(cands((0, 3.0), (90, 3.0), (210, 3.0)), 300, SMALL, "empty")
    assert (exc_info.value.gap_start_mile, exc_info.value.gap_end_mile) == (90, 210)


def test_no_stations_near_route() -> None:
    with pytest.raises(UnreachableRouteError):
        plan_fuel_stops([], 300, SMALL, "full")
    with pytest.raises(UnreachableRouteError, match="within 25 miles"):
        plan_fuel_stops([], 50, SMALL, "empty", origin_radius_miles=25)


def test_no_station_within_origin_radius_for_empty_start() -> None:
    with pytest.raises(UnreachableRouteError, match=r"nearest on this route is at mile 40\.0"):
        plan_fuel_stops(cands((40, 3.0)), 90, SMALL, "empty", origin_radius_miles=25)


def test_single_station() -> None:
    plan = plan_fuel_stops(cands((0, 3.5)), 90, SMALL, "empty")
    assert stops_of(plan) == [(0, 9.0, 31.5)]


def test_same_price_stations() -> None:
    plan = plan_fuel_stops(cands((0, 3.0), (60, 3.0), (120, 3.0), (180, 3.0)), 250, SMALL, "empty")
    assert plan.total_cost == pytest.approx(25 * 3.0)
    assert plan.gallons_purchased == pytest.approx(25.0)


def test_duplicate_stations_at_same_mile_keep_cheapest_then_closest() -> None:
    merged = prepare_candidates(
        [
            FuelCandidate(1, 30, 3.20, 2.0),
            FuelCandidate(2, 30, 3.10, 5.0),
            FuelCandidate(3, 30, 3.10, 1.0),
            FuelCandidate(4, 60, 3.00, 0.0),
        ],
        100,
    )
    assert [(c.key, c.mile) for c in merged] == [(3, 30), (4, 60)]


def test_stations_at_or_beyond_destination_are_ignored() -> None:
    assert prepare_candidates(cands((100, 1.0), (150, 1.0), (40, 3.0)), 100) == [
        FuelCandidate(2, 40, 3.0)
    ]


def test_rule3_tie_break_prefers_smaller_detour() -> None:
    stations = [
        FuelCandidate(0, 0, 2.0),
        FuelCandidate(1, 50, 3.0, off_route_miles=6.0),
        FuelCandidate(2, 70, 3.0, off_route_miles=1.0),
    ]
    plan = plan_fuel_stops(stations, 150, SMALL, "empty")
    assert [s.candidate.key for s in plan.stops] == [0, 2]


def test_start_fuel_gallons_mode() -> None:
    plan = plan_fuel_stops(cands((30, 4.0), (60, 2.0)), 150, SMALL, 5)
    # 5 gal reach mile 50: must stop at mile 30 ($4) and buy just enough to reach mile 60 ($2).
    assert stops_of(plan) == [(0, 1.0, 4.0), (1, 9.0, 18.0)]
    assert plan.initial_fuel_gallons == 5
    assert plan.initial_fuel_cost == 0.0


def test_full_mode_reports_initial_fuel_and_pump_spend_only() -> None:
    plan = plan_fuel_stops(cands((80, 3.0)), 150, SMALL, "full")
    assert stops_of(plan) == [(0, 5.0, 15.0)]
    assert (plan.initial_fuel_gallons, plan.total_cost) == (10.0, 15.0)


def test_truck_defaults_coast_to_coast_like_spacing() -> None:
    stations = cands(*[(m, 3.0 + (m % 7) / 10) for m in range(0, 2800, 120)])
    plan = plan_fuel_stops(stations, 2800, TRUCK, "empty")
    legs = np.diff([0, *[s.candidate.mile for s in plan.stops], 2800])
    assert legs.max() <= 500
    assert plan.charged_gallons == pytest.approx(280.0)


@pytest.mark.parametrize("value", ["empty", "FULL", " full ", "12.5", 12.5, 1])
def test_start_fuel_parse_valid(value) -> None:
    assert StartFuel.parse(value).mode in StartMode


@pytest.mark.parametrize("value", ["half", "", "-3", "0", "nan", "inf", None])
def test_start_fuel_parse_invalid(value) -> None:
    with pytest.raises(ValueError):
        StartFuel.parse(value)


def test_start_fuel_larger_than_tank_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds the tank"):
        plan_fuel_stops(cands((0, 3.0)), 50, SMALL, 10.5)


# --- Brute force -----------------------------------------------------------------------------


def brute_force(
    stations: list[FuelCandidate],
    total_miles: int,
    vehicle: Vehicle,
    start: StartFuel,
    origin_radius: float,
) -> float:
    """Exact DP over fuel levels in 1-mile units (0.1 gal at 10 mpg); ``inf`` if infeasible.

    Independent of the greedy: no merging, no rules; at every station it considers every
    purchase amount via ``B[f] = min_{a<=f} A[a] + (f-a)*c`` (prefix-min, O(capacity)).
    """
    assert vehicle.mpg == 10 and float(vehicle.max_range_miles).is_integer()
    cap = int(vehicle.max_range_miles)  # capacity in units
    ordered = sorted((s for s in stations if s.mile < total_miles), key=lambda s: s.mile)
    units = np.arange(cap + 1)

    def drive(levels: np.ndarray, distance: int) -> np.ndarray:
        out = np.full(cap + 1, np.inf)
        if distance <= cap:
            out[: cap + 1 - distance] = levels[distance:]
        return out

    def run(levels: np.ndarray, position: float, rest: list[FuelCandidate]) -> float:
        for s in rest:
            levels = drive(levels, int(s.mile - position))
            per_unit = s.price / 10
            levels = units * per_unit + np.minimum.accumulate(levels - units * per_unit)
            position = s.mile
        last = int(total_miles - position)
        return float(levels[last:].min()) if last <= cap else math.inf

    if start.mode is StartMode.EMPTY:
        best = math.inf
        for k, first in enumerate(ordered):
            if first.mile > origin_radius:
                continue
            levels = np.full(cap + 1, np.inf)
            levels[0] = first.mile * first.price / 10  # fuel burned to reach stop 1
            per_unit = first.price / 10
            levels = units * per_unit + np.minimum.accumulate(levels - units * per_unit)
            best = min(best, run(levels, first.mile, ordered[k + 1 :]))
        return best

    levels = np.full(cap + 1, np.inf)
    levels[round(start.initial_gallons(vehicle) * 10)] = 0.0
    return run(levels, 0.0, ordered)


def random_instance(rng: random.Random, mode: StartMode):
    vehicle = Vehicle(max_range_miles=rng.choice([40, 60, 80, 100, 120]), mpg=10)
    total = rng.randint(10, 400)
    n = rng.randint(0, 9)
    prices = [2.50, 2.75, 3.00, 3.25, 3.50, 3.75, 4.00]
    stations = [FuelCandidate(k, rng.randint(0, total - 1), rng.choice(prices)) for k in range(n)]
    radius = rng.choice([0, 10, 25, 40])
    if mode is StartMode.EMPTY:
        start = StartFuel(StartMode.EMPTY)
        if rng.random() < 0.75:  # usually give the empty start somewhere to refuel
            stations.append(
                FuelCandidate(n, rng.randint(0, min(radius, total - 1)), rng.choice(prices))
            )
    elif mode is StartMode.FULL:
        start = StartFuel(StartMode.FULL)
    else:
        start = StartFuel(StartMode.GALLONS, rng.randint(1, int(vehicle.max_range_miles)) / 10)
    return stations, total, vehicle, start, radius


@pytest.mark.parametrize("mode", list(StartMode))
def test_greedy_matches_brute_force_on_random_instances(mode: StartMode) -> None:
    rng = random.Random(f"fuel-{mode.value}")
    feasible = infeasible = 0
    for _ in range(200):
        stations, total, vehicle, start, radius = random_instance(rng, mode)
        expected = brute_force(stations, total, vehicle, start, radius)
        if math.isinf(expected):
            infeasible += 1
            with pytest.raises(UnreachableRouteError):
                plan_fuel_stops(stations, total, vehicle, start, radius)
            continue
        feasible += 1
        plan = plan_fuel_stops(stations, total, vehicle, start, radius)
        assert plan.total_cost == pytest.approx(expected, abs=1e-6), (
            stations,
            total,
            vehicle,
            start,
        )
        # Accounting invariants
        consumed = total / vehicle.mpg
        assert plan.fuel_consumed_gallons == pytest.approx(consumed)
        if mode is StartMode.EMPTY:
            assert plan.charged_gallons == pytest.approx(consumed)
        else:
            assert plan.initial_fuel_gallons + plan.gallons_purchased == pytest.approx(
                consumed + plan.fuel_at_finish_gallons
            )
        assert all(s.departure_gallons <= vehicle.tank_gallons + 1e-9 for s in plan.stops)
    # The generator must exercise both outcomes, or the test proves little.
    assert feasible >= 60 and infeasible >= 20, (feasible, infeasible)
