"""API layer: /api/v1/route/ (GET/POST), the map page, errors and throttling (ORS mocked)."""

from collections.abc import Iterator
from io import StringIO
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses
from django.conf import settings
from django.core.management import call_command
from django.urls import reverse
from rest_framework.throttling import ScopedRateThrottle

from apps.routing.serializers import (
    FuelStopSerializer,
    RouteResponseSerializer,
    SummarySerializer,
)
from apps.routing.services.corridor import StationIndex, set_station_index
from apps.stations.services.station_geocoder import read_geocoded_csv
from tests.conftest import ORS_TEST_BASE_URL, load_fixture

CAR_URL = f"{ORS_TEST_BASE_URL}/openrouteservice/v2/directions/driving-car/geojson"
ROUTE = "/api/v1/route/"
MAP = "/api/v1/route/map/"
NYC_LA = {"start": "New York, NY", "finish": "Los Angeles, CA"}
CHI_HOU = {"start": "Chicago, IL", "finish": "Houston, TX"}


@pytest.fixture(scope="module")
def station_index() -> StationIndex:
    rows = [
        (s.opis_id, s.name, s.address, s.city, s.state, float(s.price), s.lat, s.lon)
        for s in read_geocoded_csv(settings.STATIONS_GEOCODED_CSV)
    ]
    return StationIndex.from_rows(rows)


@pytest.fixture(autouse=True)
def _stations(station_index: StationIndex) -> None:
    set_station_index(station_index)


@pytest.fixture
def mocked(ors_key: str) -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


def nyc_la(mocked: responses.RequestsMock) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_nyc_la.json"))


def error_code(response) -> str:
    return response.json()["error"]["code"]


# --- Happy paths --------------------------------------------------------------------------


def test_route_get_full_response_shape(api_client, mocked) -> None:
    nyc_la(mocked)

    response = api_client.get(ROUTE, NYC_LA)

    assert response.status_code == 200, response.content
    body = response.json()
    assert set(body) == set(RouteResponseSerializer().fields)
    assert set(body["summary"]) == set(SummarySerializer().fields)
    assert all(set(stop) == set(FuelStopSerializer().fields) for stop in body["fuel_stops"])
    checked = RouteResponseSerializer(data=body)
    assert checked.is_valid(), checked.errors

    s = body["summary"]
    assert s["number_of_stops"] == len(body["fuel_stops"]) > 0
    assert s["largest_leg_miles"] <= 500
    assert s["total_fuel_cost"] > 0 and s["estimated_savings"] > 0
    assert s["stop_penalty"] == 15
    assert s["pure_cost_optimum"]["number_of_stops"] >= s["number_of_stops"]
    assert s["extra_cost_vs_pure_optimum"] >= 0
    assert s["vehicle"] == {"max_range_miles": 500, "mpg": 10, "tank_gallons": 50}
    assert body["start"]["resolved_by"] == body["finish"]["resolved_by"] == "offline"
    assert body["meta"]["external_api_calls"] == 1
    assert body["meta"]["cache_hit"] is False
    assert {"routing", "corridor", "optimize", "response", "request_total"} <= set(
        body["meta"]["timings_ms"]
    )
    assert body["meta"]["data_version"].startswith("stations-")
    assert len(body["assumptions"]) >= 5


def test_route_geojson_has_line_endpoints_and_numbered_stops(api_client, mocked) -> None:
    nyc_la(mocked)
    body = api_client.get(ROUTE, NYC_LA).json()

    features = body["route"]["features"]
    kinds = [f["properties"]["kind"] for f in features]
    assert kinds[0] == "route" and kinds[1] == "start" and kinds[-1] == "finish"
    stops = [f for f in features if f["properties"]["kind"] == "fuel_stop"]
    assert [f["properties"]["order"] for f in stops] == list(range(1, len(body["fuel_stops"]) + 1))
    line = features[0]["geometry"]["coordinates"]
    assert 5_000 < len(line) < 21_760  # thinned from the raw polyline
    lon, lat = line[0]
    assert -75 < lon < -73 and 40 < lat < 41.5  # GeoJSON (lon, lat), starts near NYC


def test_map_url_round_trips_parameters(api_client, mocked) -> None:
    nyc_la(mocked)
    body = api_client.get(ROUTE, {**NYC_LA, "mpg": 8, "start_fuel": "full"}).json()

    url = urlparse(body["map_url"])
    query = parse_qs(url.query)
    assert url.path == MAP
    assert query["start"] == ["New York, NY"] and query["mpg"] == ["8.0"]
    assert query["start_fuel"] == ["full"]


def test_repeat_request_is_a_cache_hit_with_zero_calls(api_client, mocked) -> None:
    nyc_la(mocked)
    first = api_client.get(ROUTE, NYC_LA).json()
    second = api_client.get(ROUTE, NYC_LA).json()

    assert (first["meta"]["external_api_calls"], second["meta"]["external_api_calls"]) == (1, 0)
    assert second["meta"]["cache_hit"] is True
    assert set(second["meta"]["timings_ms"]) == {"response_cache", "request_total"}
    assert second["summary"] == first["summary"]
    assert second["map_url"] == first["map_url"]
    assert len(mocked.calls) == 1


def test_cache_key_normalises_location_text(api_client, mocked) -> None:
    nyc_la(mocked)
    api_client.get(ROUTE, NYC_LA)
    again = api_client.get(ROUTE, {"start": "new york,  ny", "finish": "LOS ANGELES, CA"}).json()
    assert again["meta"]["cache_hit"] is True
    assert again["start"]["query"] == "new york,  ny"  # echoes this request's input


def test_changed_vehicle_parameters_reuse_the_route(api_client, mocked) -> None:
    nyc_la(mocked)
    api_client.get(ROUTE, NYC_LA)
    other = api_client.get(ROUTE, {**NYC_LA, "mpg": 8}).json()
    assert other["meta"]["external_api_calls"] == 0  # route cache: no ORS call
    assert other["meta"]["cache_hit"] is True
    assert "optimize" in other["meta"]["timings_ms"]  # but the plan was recomputed
    assert other["summary"]["vehicle"]["mpg"] == 8


def test_post_json_body(api_client, mocked) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))

    response = api_client.post(ROUTE, {**CHI_HOU, "start_fuel": "full"}, format="json")

    assert response.status_code == 200, response.content
    summary = response.json()["summary"]
    assert summary["start_fuel"] == "full"
    assert summary["initial_fuel_gallons"] == 50
    assert summary["initial_fuel_cost"] == 0


@pytest.mark.parametrize(("start_fuel", "label"), [("empty", "empty"), ("12.5", "12.5")])
def test_start_fuel_modes_via_api(api_client, mocked, start_fuel, label) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))
    body = api_client.get(ROUTE, {**CHI_HOU, "start_fuel": start_fuel}).json()
    assert body["summary"]["start_fuel"] == label


def test_stop_penalty_zero_returns_pure_optimum(api_client, mocked) -> None:
    nyc_la(mocked)
    s = api_client.get(ROUTE, {**NYC_LA, "stop_penalty": 0}).json()["summary"]
    assert s["extra_cost_vs_pure_optimum"] == 0
    assert s["pure_cost_optimum"]["number_of_stops"] == s["number_of_stops"]
    assert s["pure_cost_optimum"]["total_fuel_cost"] == s["total_fuel_cost"]


def test_include_geometry_false_omits_route(api_client, mocked) -> None:
    nyc_la(mocked)
    lean = api_client.get(ROUTE, {**NYC_LA, "include_geometry": "false"})
    full = api_client.get(ROUTE, NYC_LA)
    assert lean.json()["route"] is None
    assert len(lean.content) < len(full.content) / 10


# --- Validation (400) -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"finish": "Houston, TX"}, "start"),
        ({"start": "Chicago, IL"}, "finish"),
        ({**CHI_HOU, "start_fuel": "half"}, "start_fuel"),
        ({**CHI_HOU, "start_fuel": "-5"}, "start_fuel"),
        ({**CHI_HOU, "start_fuel": "60"}, "start_fuel"),  # > 50-gallon tank
        ({**CHI_HOU, "mpg": "0"}, "mpg"),
        ({**CHI_HOU, "max_range_miles": "20"}, "max_range_miles"),
        ({**CHI_HOU, "corridor_miles": "100"}, "corridor_miles"),
        ({**CHI_HOU, "stop_penalty": "500"}, "stop_penalty"),
        ({**CHI_HOU, "profile": "cycling-road"}, "profile"),
    ],
)
def test_invalid_input_returns_400_with_field_details(api_client, params, field) -> None:
    response = api_client.get(ROUTE, params)
    assert response.status_code == 400
    assert error_code(response) == "invalid_input"
    assert field in response.json()["error"]["details"]


def test_invalid_json_body(api_client) -> None:
    response = api_client.post(ROUTE, "{not json", content_type="application/json")
    assert response.status_code == 400
    assert error_code(response) == "parse_error"


def test_method_not_allowed_uses_error_envelope(api_client) -> None:
    response = api_client.put(ROUTE, {})
    assert response.status_code == 405
    assert error_code(response) == "method_not_allowed"


# --- Domain errors (422) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"start": "Toronto, ON", "finish": "Chicago, IL"}, "location_outside_usa"),
        ({"start": "43.6532,-79.3832", "finish": "Chicago, IL"}, "location_outside_usa"),
        ({"start": "Dallas, TX", "finish": "Dallas, Texas"}, "same_location"),
    ],
)
def test_location_errors_make_no_routing_call(api_client, mocked, params, code) -> None:
    response = api_client.get(ROUTE, params)
    assert response.status_code == 422
    assert error_code(response) == code
    assert len(mocked.calls) == 0


def test_unroutable_trip_is_422(api_client, mocked) -> None:
    mocked.post(CAR_URL, status=404, json={"error": {"code": 2009, "message": "Route not found"}})
    response = api_client.get(ROUTE, {"start": "Honolulu, HI", "finish": "Los Angeles, CA"})
    assert response.status_code == 422
    assert error_code(response) == "route_unreachable"


def test_fuel_gap_is_422_with_gap_details(api_client, mocked) -> None:
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))
    response = api_client.get(ROUTE, {**CHI_HOU, "max_range_miles": 50, "corridor_miles": 1})
    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert error_code(response) == "route_unreachable"
    assert {"gap_start_mile", "gap_end_mile", "gap_start_near", "gap_end_near"} <= set(details)


# --- Provider errors (429 / 502 / 503 / 504) --------------------------------------------------


@pytest.mark.parametrize(
    ("mock_kwargs", "status", "code"),
    [
        ({"status": 429, "json": {"error": "Rate limit exceeded"}}, 429, "rate_limited"),
        ({"status": 401, "json": {"error": "Invalid key"}}, 503, "routing_provider_unavailable"),
        ({"status": 503}, 503, "routing_provider_unavailable"),
        ({"status": 500, "json": {"error": {"code": 2099}}}, 502, "routing_provider_error"),
        ({"body": requests.ReadTimeout("slow")}, 504, "routing_provider_timeout"),
    ],
)
def test_provider_errors_are_mapped(api_client, mocked, mock_kwargs, status, code) -> None:
    mocked.post(CAR_URL, **mock_kwargs)
    response = api_client.get(ROUTE, CHI_HOU)
    assert response.status_code == status
    assert error_code(response) == code


def test_missing_api_key_is_503(api_client, settings) -> None:
    settings.ORS_API_KEY = ""
    response = api_client.get(ROUTE, CHI_HOU)
    assert response.status_code == 503
    assert error_code(response) == "routing_provider_unavailable"
    assert "ORS_API_KEY" in response.json()["error"]["message"]


def test_no_stations_loaded_is_503(api_client) -> None:
    set_station_index(StationIndex.from_rows([]))
    response = api_client.get(ROUTE, CHI_HOU)
    assert response.status_code == 503
    assert error_code(response) == "station_data_unavailable"


# --- Throttling -------------------------------------------------------------------------------


def test_route_endpoint_is_throttled(api_client, mocked, monkeypatch) -> None:
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"route": "2/min"})
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))

    statuses = [api_client.get(ROUTE, CHI_HOU).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]
    throttled = api_client.get(ROUTE, CHI_HOU)
    assert error_code(throttled) == "rate_limited"
    assert int(throttled["Retry-After"]) > 0


def test_health_is_not_throttled(api_client, monkeypatch) -> None:
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"route": "1/min"})
    assert all(api_client.get(reverse("health")).status_code == 200 for _ in range(3))


# --- Map page -----------------------------------------------------------------------------------


def test_map_after_json_makes_zero_extra_calls(api_client, mocked) -> None:
    nyc_la(mocked)
    map_url = api_client.get(ROUTE, NYC_LA).json()["map_url"]

    page = api_client.get(map_url)

    assert page.status_code == 200
    assert page["Content-Type"].startswith("text/html")
    html = page.content.decode()
    assert 'id="trip-data"' in html and "leaflet.min.js" in html
    assert "routing API calls for this page" in html
    assert len(mocked.calls) == 1  # the map reused the cached route


def test_map_escapes_data(api_client, mocked) -> None:
    nyc_la(mocked)
    html = api_client.get(MAP, {**NYC_LA, "start": "New York, NY"}).content.decode()
    trip_json = html.split('id="trip-data" type="application/json">')[1].split("</script>")[0]
    assert "<" not in trip_json  # json_script escapes <, >, & so data cannot break out


def test_map_tiles_send_an_origin_referrer(api_client, mocked) -> None:
    # OSM blocks tile requests without a Referer, and Django's default same-origin policy
    # sends none cross-origin, so the tile layer must opt in (ADR-021).
    nyc_la(mocked)
    html = api_client.get(MAP, NYC_LA).content.decode()
    assert "https://tile.openstreetmap.org/{z}/{x}/{y}.png" in html
    assert 'referrerPolicy: "strict-origin-when-cross-origin"' in html
    assert "OpenStreetMap</a> contributors" in html


def test_map_error_page(api_client) -> None:
    page = api_client.get(MAP, {"start": "Toronto, ON", "finish": "Chicago, IL"})
    assert page.status_code == 422
    html = page.content.decode()
    assert "Could not build the route map" in html and "location_outside_usa" in html


# --- Health and schema ----------------------------------------------------------------------


def test_health_reports_in_memory_station_count(api_client, station_index) -> None:
    assert api_client.get(reverse("health")).json()["stations_loaded"] == len(station_index)


def test_openapi_schema_is_valid_and_documents_route_endpoints(api_client) -> None:
    out = StringIO()
    call_command("spectacular", "--validate", "--fail-on-warn", stdout=out)
    schema = out.getvalue()
    assert "/api/v1/route/" in schema and "/api/v1/route/map/" in schema
    assert "stop_penalty" in schema and "pure_cost_optimum" in schema


def test_read_timeout_retried_once_is_reported_as_one_call_two_attempts(api_client, mocked) -> None:
    mocked.post(CAR_URL, body=requests.ReadTimeout("ORS stalled"))
    mocked.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))

    meta = api_client.get(ROUTE, CHI_HOU).json()["meta"]

    assert (meta["external_api_calls"], meta["external_api_attempts"]) == (1, 2)
