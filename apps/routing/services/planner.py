"""Trip planning orchestrator: locations -> route -> corridor -> optimal fuel plan.

This is the only module that wires the pieces together; views stay thin and the
geometry/corridor/optimizer modules stay pure.

External calls per request: 0 for "City, ST"/"lat,lon" inputs when the route is cached,
1 for a new route, plus at most 1 geocode per free-text address (also cached).
"""

import hashlib
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np
from django.conf import settings
from django.core.cache import cache

from apps.routing.exceptions import (
    InvalidInputError,
    RouteUnreachableError,
    SameLocationError,
    StationDataUnavailableError,
)
from apps.routing.services.corridor import (
    Corridor,
    StationIndex,
    StationRecord,
    find_candidates,
    get_station_index,
)
from apps.routing.services.geo import METERS_PER_MILE, haversine_miles
from apps.routing.services.geocoding import (
    ResolvedLocation,
    USGeography,
    get_us_geography,
    resolve_location,
)
from apps.routing.services.optimizer import (
    FuelCandidate,
    FuelPlan,
    FuelStop,
    StartFuel,
    UnreachableRouteError,
    Vehicle,
    plan_fuel_stops,
)
from apps.routing.services.ors_client import ORSClient, Route, count_external_calls, get_ors_client
from apps.routing.services.route_geometry import RouteGeometry, build_geometry, resample

logger = logging.getLogger(__name__)

SAME_LOCATION_MILES = 0.5


@dataclass(frozen=True, slots=True)
class PlanParams:
    """Validated inputs for one trip."""

    start: str
    finish: str
    start_fuel: StartFuel = field(default_factory=lambda: StartFuel.parse("empty"))
    max_range_miles: float = 500.0
    mpg: float = 10.0
    corridor_miles: float = 10.0
    origin_radius_miles: float = 25.0
    profile: str = "driving-car"
    stop_penalty: float = field(default_factory=lambda: settings.DEFAULT_STOP_PENALTY)

    @property
    def vehicle(self) -> Vehicle:
        return Vehicle(max_range_miles=self.max_range_miles, mpg=self.mpg)


@dataclass(frozen=True, slots=True)
class PlannedStop:
    """A fuel stop joined with its station details."""

    order: int
    station: StationRecord
    lat: float
    lon: float
    price_per_gallon: float
    mile_marker: float
    off_route_miles: float
    fuel_on_arrival_gallons: float
    gallons_purchased: float
    cost: float
    fuel_on_departure_gallons: float


@dataclass(frozen=True)
class TripPlan:
    """Everything the API needs to render a response (values unrounded)."""

    params: PlanParams
    start: ResolvedLocation
    finish: ResolvedLocation
    route: Route
    fuel_plan: FuelPlan
    pure_optimum: FuelPlan  # same trip with stop_penalty = 0 (cheapest fuel, any stops)
    stops: tuple[PlannedStop, ...]
    stations_considered: int
    corridor_average_price: float
    max_leg_miles: float
    external_api_calls: int
    route_cache_hit: bool
    timings_ms: dict[str, float]

    @property
    def total_miles(self) -> float:
        return self.route.distance_m / METERS_PER_MILE

    @property
    def duration_hours(self) -> float:
        return self.route.duration_s / 3600

    @property
    def baseline_cost(self) -> float:
        """Paying the corridor's average price for every paid gallon."""
        return self.fuel_plan.charged_gallons * self.corridor_average_price

    @property
    def estimated_savings(self) -> float:
        return self.baseline_cost - self.fuel_plan.total_cost

    @property
    def extra_cost_vs_pure_optimum(self) -> float:
        """Fuel money traded for fewer stops (0 when stop_penalty is 0)."""
        return self.fuel_plan.total_cost - self.pure_optimum.total_cost


class _Timer:
    """Collects named wall-clock sections in milliseconds."""

    def __init__(self) -> None:
        self.ms: dict[str, float] = {}

    @contextmanager
    def section(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.ms[name] = self.ms.get(name, 0.0) + 1000 * (time.perf_counter() - started)


def route_cache_key(start: ResolvedLocation, finish: ResolvedLocation, profile: str) -> str:
    """Cache key for a route: profile + endpoints rounded to ~1 m."""
    raw = f"{profile}|{start.lat:.5f},{start.lon:.5f}|{finish.lat:.5f},{finish.lon:.5f}"
    return "route:v1:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_route(
    start: ResolvedLocation, finish: ResolvedLocation, profile: str, client: ORSClient
) -> tuple[Route, bool]:
    """Route between two points, from the cache if possible. Returns ``(route, cache_hit)``."""
    key = route_cache_key(start, finish, profile)
    cached = cache.get(key)
    if cached is not None:
        logger.info("Route cache hit (%s -> %s, %s)", start.name, finish.name, profile)
        return cached, True
    route = client.directions([(start.lat, start.lon), (finish.lat, finish.lon)], profile)
    cache.set(key, route, settings.ROUTE_CACHE_TTL_SECONDS)
    return route, False


def plan_trip(
    params: PlanParams,
    *,
    client: ORSClient | None = None,
    geography: USGeography | None = None,
    stations: StationIndex | None = None,
) -> TripPlan:
    """Plan the cheapest refuelling for a trip. Raises ``DomainError`` subclasses."""
    timer = _Timer()
    geography = geography or get_us_geography()
    stations = stations if stations is not None else get_station_index()
    if len(stations) == 0:
        raise StationDataUnavailableError()
    client = client or get_ors_client()

    with timer.section("total"), count_external_calls() as calls:
        with timer.section("geocode"):
            start = resolve_location(params.start, geography=geography, client=client)
            finish = resolve_location(params.finish, geography=geography, client=client)
        separation = float(haversine_miles(start.lat, start.lon, finish.lat, finish.lon))
        if separation < SAME_LOCATION_MILES:
            raise SameLocationError(
                f"'{params.start}' and '{params.finish}' resolve to the same place.",
                start=start.name,
                finish=finish.name,
            )

        with timer.section("routing"):
            route, cache_hit = get_route(start, finish, params.profile, client)

        with timer.section("geometry"):
            geometry = build_geometry(route.coordinates, route.distance_m / METERS_PER_MILE)
            dense = resample(geometry)

        with timer.section("corridor"):
            corridor = find_candidates(dense, stations, params.corridor_miles)

        with timer.section("optimize"):
            fuel_plan = _optimize(params, corridor, geometry, geography, params.stop_penalty)
        if params.stop_penalty > 0:
            with timer.section("pure_optimum"):
                pure_optimum = _optimize(params, corridor, geometry, geography, 0.0)
        else:
            pure_optimum = fuel_plan

    stops = tuple(_planned_stop(n, s, stations) for n, s in enumerate(fuel_plan.stops, start=1))
    legs = np.diff([0.0, *(s.mile_marker for s in stops), geometry.total_miles])
    return TripPlan(
        params=params,
        start=start,
        finish=finish,
        route=route,
        fuel_plan=fuel_plan,
        pure_optimum=pure_optimum,
        stops=stops,
        stations_considered=len(corridor),
        corridor_average_price=float(corridor.price.mean()) if len(corridor) else 0.0,
        max_leg_miles=float(legs.max()),
        external_api_calls=calls.count,
        route_cache_hit=cache_hit,
        timings_ms=timer.ms,
    )


def _optimize(
    params: PlanParams,
    corridor: Corridor,
    geometry: RouteGeometry,
    geography: USGeography,
    stop_penalty: float,
) -> FuelPlan:
    candidates = [
        FuelCandidate(key=int(i), mile=float(m), price=float(p), off_route_miles=float(o))
        for i, m, p, o in zip(
            corridor.station_idx,
            corridor.mile,
            corridor.price,
            corridor.off_route_miles,
            strict=True,
        )
    ]
    try:
        return plan_fuel_stops(
            candidates,
            geometry.total_miles,
            params.vehicle,
            params.start_fuel,
            params.origin_radius_miles,
            stop_penalty,
        )
    except ValueError as exc:  # e.g. start_fuel larger than the tank
        raise InvalidInputError(str(exc)) from exc
    except UnreachableRouteError as exc:
        raise RouteUnreachableError(
            str(exc),
            gap_start_mile=round(exc.gap_start_mile, 1),
            gap_end_mile=round(exc.gap_end_mile, 1),
            gap_start_near=_near(geometry, exc.gap_start_mile, geography),
            gap_end_near=_near(geometry, exc.gap_end_mile, geography),
            stations_in_corridor=len(candidates),
        ) from exc


def _near(geometry: RouteGeometry, mile: float, geography: USGeography) -> str:
    lat, lon = geometry.point_at(mile)
    place, _ = geography.nearest_place(lat, lon)
    return f"{place.display_name}, {place.state}"


def _planned_stop(order: int, stop: FuelStop, stations: StationIndex) -> PlannedStop:
    i = stop.candidate.key
    return PlannedStop(
        order=order,
        station=stations.records[i],
        lat=float(stations.lat[i]),
        lon=float(stations.lon[i]),
        price_per_gallon=stop.candidate.price,
        mile_marker=stop.candidate.mile,
        off_route_miles=stop.candidate.off_route_miles,
        fuel_on_arrival_gallons=stop.arrival_gallons,
        gallons_purchased=stop.purchased_gallons,
        cost=stop.cost,
        fuel_on_departure_gallons=stop.departure_gallons,
    )
