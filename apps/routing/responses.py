"""Build API payloads from a :class:`TripPlan`. All rounding happens here, and only here."""

import hashlib
from functools import lru_cache
from typing import Any

import numpy as np
from django.conf import settings

from apps.routing.services.geo import haversine_miles
from apps.routing.services.geocoding import ResolvedLocation
from apps.routing.services.optimizer import FuelPlan, StartMode
from apps.routing.services.ors_client import Route
from apps.routing.services.planner import TripPlan

API_VERSION = "v1"


def money(value: float) -> float:
    return round(value + 0.0, 2)


def gallons(value: float) -> float:
    return round(value + 0.0, 2)


def miles(value: float) -> float:
    return round(value + 0.0, 1)


@lru_cache(maxsize=1)
def data_version() -> str:
    """Identifies the station dataset (hash of the committed geocoded CSV)."""
    digest = hashlib.sha256(settings.STATIONS_GEOCODED_CSV.read_bytes()).hexdigest()[:10]
    return f"stations-{digest}"


def display_coordinates(route: Route, spacing_miles: float) -> list[list[float]]:
    """Route vertices thinned to at least ``spacing_miles`` apart along the path, as
    ``[lon, lat]`` (GeoJSON order), rounded to 5 decimals (~1 m). First and last kept."""
    coords = np.asarray(route.coordinates, dtype=np.float64)
    if len(coords) <= 2:
        return np.round(coords, 5).tolist()
    steps = haversine_miles(coords[:-1, 1], coords[:-1, 0], coords[1:, 1], coords[1:, 0])
    along = np.concatenate(([0.0], np.cumsum(steps)))
    _, keep = np.unique(np.floor(along / spacing_miles), return_index=True)
    keep = np.union1d(keep, [len(coords) - 1])
    return np.round(coords[keep], 5).tolist()


def _summary(trip: TripPlan) -> dict[str, Any]:
    plan: FuelPlan = trip.fuel_plan
    vehicle = trip.params.vehicle
    return {
        "total_distance_miles": miles(trip.total_miles),
        "estimated_duration_hours": round(trip.duration_hours, 1),
        "fuel_consumed_gallons": gallons(plan.fuel_consumed_gallons),
        "gallons_purchased": gallons(plan.gallons_purchased),
        "initial_fuel_gallons": gallons(plan.initial_fuel_gallons),
        "initial_fuel_cost": money(plan.initial_fuel_cost),
        "total_fuel_cost": money(plan.total_cost),
        "average_price_paid": round(plan.average_price_paid, 3),
        "average_corridor_price": round(trip.corridor_average_price, 3),
        "baseline_cost_at_avg_corridor_price": money(trip.baseline_cost),
        "estimated_savings": money(trip.estimated_savings),
        "number_of_stops": len(trip.stops),
        "largest_leg_miles": miles(trip.max_leg_miles),
        "fuel_at_finish_gallons": gallons(plan.fuel_at_finish_gallons),
        "stop_penalty": trip.params.stop_penalty,
        "pure_cost_optimum": {
            "total_fuel_cost": money(trip.pure_optimum.total_cost),
            "number_of_stops": len(trip.pure_optimum.stops),
        },
        "extra_cost_vs_pure_optimum": money(trip.extra_cost_vs_pure_optimum),
        "vehicle": {
            "max_range_miles": vehicle.max_range_miles,
            "mpg": vehicle.mpg,
            "tank_gallons": round(vehicle.tank_gallons, 2),
        },
        "start_fuel": str(plan.start_fuel),
    }


def _stops(trip: TripPlan) -> list[dict[str, Any]]:
    return [
        {
            "order": s.order,
            "station_id": s.station.opis_id,
            "name": s.station.name,
            "address": s.station.address,
            "city": s.station.city,
            "state": s.station.state,
            "lat": round(s.lat, 5),
            "lon": round(s.lon, 5),
            "price_per_gallon": round(s.price_per_gallon, 3),
            "mile_marker": miles(s.mile_marker),
            "off_route_miles": miles(s.off_route_miles),
            "fuel_on_arrival_gallons": gallons(s.fuel_on_arrival_gallons),
            "gallons_purchased": gallons(s.gallons_purchased),
            "cost": money(s.cost),
            "fuel_on_departure_gallons": gallons(s.fuel_on_departure_gallons),
        }
        for s in trip.stops
    ]


def _point(kind: str, lat: float, lon: float, **properties: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
        "properties": {"kind": kind, **properties},
    }


def _geojson(trip: TripPlan, stops: list[dict[str, Any]]) -> dict[str, Any]:
    line = {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": display_coordinates(
                trip.route, settings.RESPONSE_GEOMETRY_SPACING_MILES
            ),
        },
        "properties": {
            "kind": "route",
            "distance_miles": miles(trip.total_miles),
            "profile": trip.params.profile,
        },
    }
    features = [
        line,
        _point("start", trip.start.lat, trip.start.lon, name=trip.start.name),
        *(
            _point(
                "fuel_stop",
                s["lat"],
                s["lon"],
                order=s["order"],
                name=s["name"],
                city=f"{s['city']}, {s['state']}",
                price_per_gallon=s["price_per_gallon"],
                gallons_purchased=s["gallons_purchased"],
                cost=s["cost"],
                mile_marker=s["mile_marker"],
            )
            for s in stops
        ),
        _point("finish", trip.finish.lat, trip.finish.lon, name=trip.finish.name),
    ]
    return {"type": "FeatureCollection", "features": features}


def assumptions(trip: TripPlan) -> list[str]:
    """Plain-English assumptions behind the numbers (also shown on the map page)."""
    p, plan = trip.params, trip.fuel_plan
    vehicle = p.vehicle
    if plan.start_fuel.mode is StartMode.EMPTY:
        start = (
            f"The vehicle starts with just enough fuel to reach its first stop (within "
            f"{p.origin_radius_miles:g} miles); that fuel is charged at the first stop's price, "
            "so total_fuel_cost covers every gallon burned on the trip."
        )
    else:
        start = (
            f"The vehicle starts with {plan.initial_fuel_gallons:g} gallons that are not charged; "
            "total_fuel_cost is the money spent at the pump on this trip."
        )
    if p.stop_penalty > 0:
        stops = (
            f"Stops minimise fuel cost + ${p.stop_penalty:g} per stop. The cheapest possible plan "
            f"costs ${trip.pure_optimum.total_cost:,.2f} with {len(trip.pure_optimum.stops)} "
            "stops (summary.pure_cost_optimum); set stop_penalty=0 to use it."
        )
    else:
        stops = "Stops minimise fuel cost only (stop_penalty=0): the cheapest possible plan."
    return [
        "Fuel prices are the retail prices in the provided OPIS CSV (US stations only; "
        "duplicate station IDs merged at their lowest price).",
        f"Station locations are city-level (city centroid), not the exact highway exit; "
        f"stations within {p.corridor_miles:g} miles of the route are considered.",
        "The short detour from the route to a station is not added to fuel use.",
        f"Fuel use is linear at {vehicle.mpg:g} mpg with a {vehicle.max_range_miles:g}-mile "
        f"range ({vehicle.tank_gallons:g}-gallon tank); any amount can be bought at a stop.",
        start,
        stops,
        "The vehicle arrives at the destination with an (almost) empty tank.",
    ]


def build_route_response(
    trip: TripPlan, *, map_url: str, include_geometry: bool, timings_ms: dict[str, float]
) -> dict[str, Any]:
    """The ``/api/v1/route/`` JSON body (see ``RouteResponseSerializer``)."""
    stops = _stops(trip)
    return {
        "start": _location(trip.start),
        "finish": _location(trip.finish),
        "summary": _summary(trip),
        "fuel_stops": stops,
        "route": _geojson(trip, stops) if include_geometry else None,
        "map_url": map_url,
        "assumptions": assumptions(trip),
        "meta": {
            "external_api_calls": trip.external_api_calls,
            "external_api_attempts": trip.external_api_attempts,
            "cache_hit": trip.route_cache_hit,
            "timings_ms": {k: round(v, 1) for k, v in timings_ms.items()},
            "stations_considered": trip.stations_considered,
            "corridor_miles": trip.params.corridor_miles,
            "profile": trip.params.profile,
            "data_version": data_version(),
            "api_version": API_VERSION,
        },
    }


def _location(location: ResolvedLocation) -> dict[str, Any]:
    return {
        "query": location.query,
        "name": location.name,
        "lat": round(location.lat, 5),
        "lon": round(location.lon, 5),
        "resolved_by": location.resolved_by,
    }


def build_map_context(trip: TripPlan, *, json_url: str) -> dict[str, Any]:
    """Data embedded in the Leaflet page (Leaflet wants ``[lat, lon]``)."""
    stops = _stops(trip)
    coords = display_coordinates(trip.route, settings.RESPONSE_GEOMETRY_SPACING_MILES)
    return {
        "trip": {
            "start": _location(trip.start),
            "finish": _location(trip.finish),
            "summary": _summary(trip),
            "stops": stops,
            "line": [[lat, lon] for lon, lat in coords],
            "assumptions": assumptions(trip),
            "external_api_calls": trip.external_api_calls,
            "cache_hit": trip.route_cache_hit,
        },
        "json_url": json_url,
    }
