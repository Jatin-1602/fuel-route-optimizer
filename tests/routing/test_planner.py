"""End-to-end planning on real recorded ORS geometry and the real station data (0 calls)."""

from collections.abc import Iterator

import numpy as np
import pytest
import responses
from django.conf import settings

from apps.routing.exceptions import (
    InvalidInputError,
    LocationOutsideUSAError,
    RouteUnreachableError,
    RoutingProviderError,
    SameLocationError,
    StationDataUnavailableError,
)
from apps.routing.services.corridor import StationIndex
from apps.routing.services.optimizer import StartFuel
from apps.routing.services.ors_client import ORSClient
from apps.routing.services.planner import PlanParams, plan_trip
from apps.stations.services.station_geocoder import read_geocoded_csv
from tests.conftest import ORS_TEST_BASE_URL, load_fixture

CAR_URL = f"{ORS_TEST_BASE_URL}/openrouteservice/v2/directions/driving-car/geojson"
HGV_URL = f"{ORS_TEST_BASE_URL}/openrouteservice/v2/directions/driving-hgv/geojson"


@pytest.fixture(scope="module")
def stations() -> StationIndex:
    """All committed geocoded stations, without touching the database."""
    rows = [
        (s.opis_id, s.name, s.address, s.city, s.state, float(s.price), s.lat, s.lon)
        for s in read_geocoded_csv(settings.STATIONS_GEOCODED_CSV)
    ]
    return StationIndex.from_rows(rows)


@pytest.fixture
def client(ors_key: str) -> ORSClient:
    return ORSClient(ors_key, ORS_TEST_BASE_URL, backoff_seconds=0)


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


def plan(stations, client, start="New York, NY", finish="Los Angeles, CA", **kwargs):
    return plan_trip(PlanParams(start, finish, **kwargs), client=client, stations=stations)


# --- NYC -> LA on the recorded route ------------------------------------------------------


def test_nyc_to_la_plan_is_feasible_and_consistent(stations, client, mocked) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_nyc_la.json"))

    trip = plan(stations, client)
    fp = trip.fuel_plan

    assert trip.external_api_calls == 1
    assert trip.route_cache_hit is False
    assert trip.total_miles == pytest.approx(2811.6, abs=0.1)
    assert trip.stations_considered > 100
    assert len(trip.stops) >= 6  # 2,800 miles / 500-mile range
    legs = np.diff([0, *(s.mile_marker for s in trip.stops), trip.total_miles])
    assert legs.max() <= 500 + 1e-6
    assert trip.max_leg_miles == pytest.approx(legs.max())
    assert [s.mile_marker for s in trip.stops] == sorted(s.mile_marker for s in trip.stops)
    assert all(s.off_route_miles <= 10 for s in trip.stops)
    assert all(s.fuel_on_departure_gallons <= 50 + 1e-9 for s in trip.stops)
    assert fp.charged_gallons == pytest.approx(trip.total_miles / 10)  # empty: every gallon paid
    assert sum(s.cost for s in trip.stops) + fp.initial_fuel_cost == pytest.approx(fp.total_cost)
    assert 2.5 < fp.average_price_paid < trip.corridor_average_price
    assert trip.estimated_savings > 0
    assert trip.stops[0].mile_marker <= 25  # empty start: first stop within origin radius
    assert {"geocode", "routing", "geometry", "corridor", "optimize", "total"} <= set(
        trip.timings_ms
    )


def test_repeat_request_hits_route_cache_with_zero_calls(stations, client, mocked) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_nyc_la.json"))
    first = plan(stations, client)

    again = plan(stations, client)
    other_vehicle = plan(stations, client, mpg=8, start_fuel=StartFuel.parse("full"))

    assert (first.external_api_calls, again.external_api_calls) == (1, 0)
    assert again.route_cache_hit and other_vehicle.route_cache_hit
    assert other_vehicle.external_api_calls == 0  # vehicle params do not invalidate the route
    assert again.fuel_plan.total_cost == pytest.approx(first.fuel_plan.total_cost)
    assert len(mocked.calls) == 1


def test_processing_time_is_well_under_budget(stations, client, mocked) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_nyc_la.json"))
    trip = plan(stations, client)
    own = trip.timings_ms["geometry"] + trip.timings_ms["corridor"] + trip.timings_ms["optimize"]
    assert own < 300  # PROMPT §3.9 target; typically ~25 ms


# --- Chicago -> Houston: all start-fuel modes ---------------------------------------------


@pytest.mark.parametrize("start_fuel", ["empty", "full", "12.5"])
def test_chicago_to_houston_all_start_fuel_modes(stations, client, mocked, start_fuel) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))

    trip = plan(
        stations, client, "Chicago, IL", "Houston, TX", start_fuel=StartFuel.parse(start_fuel)
    )
    fp = trip.fuel_plan

    consumed = trip.total_miles / 10
    assert fp.fuel_consumed_gallons == pytest.approx(consumed)
    assert trip.max_leg_miles <= 500
    if start_fuel == "empty":
        assert fp.charged_gallons == pytest.approx(consumed)
        assert fp.initial_fuel_cost > 0
    else:
        initial = 50.0 if start_fuel == "full" else 12.5
        assert fp.initial_fuel_gallons == initial
        assert fp.initial_fuel_cost == 0
        assert initial + fp.gallons_purchased == pytest.approx(consumed + fp.fuel_at_finish_gallons)
        assert fp.fuel_at_finish_gallons == pytest.approx(0, abs=1e-6)


def test_full_tank_costs_less_than_empty(stations, client, mocked) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))
    empty = plan(stations, client, "Chicago, IL", "Houston, TX")
    full = plan(stations, client, "Chicago, IL", "Houston, TX", start_fuel=StartFuel.parse("full"))
    assert full.fuel_plan.total_cost < empty.fuel_plan.total_cost


def test_hgv_profile_uses_hgv_endpoint(stations, client, mocked) -> None:
    mocked.post(HGV_URL, json=load_fixture("directions_chicago_houston.json"))
    trip = plan(stations, client, "Chicago, IL", "Houston, TX", profile="driving-hgv")
    assert trip.external_api_calls == 1
    assert mocked.calls[0].request.url == HGV_URL


# --- Errors -------------------------------------------------------------------------------


def test_same_location_rejected_before_routing(stations, client, mocked) -> None:
    with pytest.raises(SameLocationError):
        plan(stations, client, "Dallas, TX", "dallas, texas")
    assert len(mocked.calls) == 0


def test_outside_usa_rejected_before_routing(stations, client, mocked) -> None:
    with pytest.raises(LocationOutsideUSAError):
        plan(stations, client, "Toronto, ON", "Chicago, IL")
    assert len(mocked.calls) == 0


def test_hawaii_route_reported_unreachable(stations, client, mocked) -> None:
    body = {"error": {"code": 2009, "message": "Route could not be found"}}
    mocked.post(CAR_URL, status=404, json=body)
    with pytest.raises(RouteUnreachableError):
        plan(stations, client, "Honolulu, HI", "Los Angeles, CA")


def test_alaska_route_through_canada_has_fuel_gap(stations, client, mocked) -> None:
    # ORS routes Anchorage -> Seattle through Canada, where the data has no stations.
    straight = {
        "type": "FeatureCollection",
        "bbox": [-149.9, 47.6, -122.3, 61.2],
        "features": [
            {
                "type": "Feature",
                "properties": {"summary": {"distance": 3_600_000.0, "duration": 150_000.0}},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-149.9, 61.2], [-135.0, 60.7], [-122.3, 47.6]],
                },
            }
        ],
    }
    mocked.post(CAR_URL, json=straight)
    with pytest.raises(RouteUnreachableError) as exc_info:
        plan(stations, client, "Anchorage, AK", "Seattle, WA")
    details = exc_info.value.details
    assert details["gap_end_mile"] - details["gap_start_mile"] > 500
    assert details["gap_start_near"].endswith(", AK")


def test_start_fuel_above_tank_is_invalid_input(stations, client, mocked) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))
    with pytest.raises(InvalidInputError, match="exceeds the tank"):
        plan(stations, client, "Chicago, IL", "Houston, TX", start_fuel=StartFuel.parse("60"))


def test_no_stations_loaded(client) -> None:
    with pytest.raises(StationDataUnavailableError):
        plan(StationIndex.from_rows([]), client)


# --- Stop penalty ------------------------------------------------------------------------


def test_default_stop_penalty_trades_little_money_for_far_fewer_stops(
    stations, client, mocked
) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_nyc_la.json"))

    trip = plan(stations, client)

    assert trip.params.stop_penalty == settings.DEFAULT_STOP_PENALTY == 15
    assert len(trip.stops) < len(trip.pure_optimum.stops)
    assert trip.pure_optimum.stop_penalty == 0
    assert (
        0 <= trip.extra_cost_vs_pure_optimum < 15 * (len(trip.pure_optimum.stops) - len(trip.stops))
    )  # never pays more than the stops it saves
    assert trip.max_leg_miles <= 500
    assert trip.timings_ms["optimize"] < 250  # ~35 ms locally; generous for CI


def test_zero_stop_penalty_is_the_pure_optimum(stations, client, mocked) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))
    trip = plan(stations, client, "Chicago, IL", "Houston, TX", stop_penalty=0)
    assert trip.pure_optimum is trip.fuel_plan
    assert trip.extra_cost_vs_pure_optimum == 0
    assert "pure_optimum" not in trip.timings_ms


@pytest.mark.parametrize("start_fuel", ["empty", "full", "12.5"])
def test_stop_penalty_all_start_fuel_modes_on_real_route(
    stations, client, mocked, start_fuel
) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))
    trip = plan(
        stations, client, "Chicago, IL", "Houston, TX", start_fuel=StartFuel.parse(start_fuel)
    )
    fp, pure = trip.fuel_plan, trip.pure_optimum
    assert fp.objective <= pure.total_cost + 15 * len(pure.stops) + 1e-6
    assert fp.total_cost >= pure.total_cost - 1e-6
    assert trip.max_leg_miles <= 500


def test_degenerate_route_from_provider_is_a_502_style_error(stations, client, mocked) -> None:
    one_point = load_fixture("directions_chicago_houston.json")
    one_point["features"][0]["geometry"]["coordinates"] = [[-87.68, 41.84]]
    mocked.post(CAR_URL, json=one_point)
    with pytest.raises(RoutingProviderError, match="unusable route"):
        plan(stations, client, "Chicago, IL", "Houston, TX")
