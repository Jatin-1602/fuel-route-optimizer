"""ORS client: parsing, retries, error mapping and call counting (HTTP mocked with `responses`)."""

import json
from collections.abc import Iterator

import pytest
import requests
import responses

from apps.routing.exceptions import (
    RateLimitedError,
    RouteUnreachableError,
    RoutingNotConfiguredError,
    RoutingProviderError,
    RoutingProviderTimeoutError,
    RoutingProviderUnavailableError,
)
from apps.routing.services.ors_client import (
    ORSClient,
    count_external_calls,
    get_ors_client,
)
from tests.conftest import ORS_TEST_BASE_URL, load_fixture

DIRECTIONS_URL = f"{ORS_TEST_BASE_URL}/openrouteservice/v2/directions/driving-car/geojson"
HGV_URL = f"{ORS_TEST_BASE_URL}/openrouteservice/v2/directions/driving-hgv/geojson"
SEARCH_URL = f"{ORS_TEST_BASE_URL}/pelias/v1/search"
STRUCTURED_URL = f"{ORS_TEST_BASE_URL}/pelias/v1/search/structured"
DALLAS = (32.793333, -96.766513)
DENVER = (39.76185, -104.881105)


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    """Intercept all HTTP; any unexpected real request raises ConnectionError."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def client(ors_key: str) -> ORSClient:
    return ORSClient(ors_key, ORS_TEST_BASE_URL, max_retries=2, backoff_seconds=0)


# --- Success paths ------------------------------------------------------------------------


def test_directions_parses_real_fixture_and_sends_lon_lat(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, json=load_fixture("directions_dallas_denver.json"))

    route = client.directions([DALLAS, DENVER])

    assert route.distance_m == pytest.approx(1_291_103.4)
    assert route.duration_s == pytest.approx(47_860.3)
    assert len(route.coordinates) == 6116
    lon, lat = route.coordinates[0]
    assert (lon, lat) == pytest.approx((-96.76642, 32.79325))  # GeoJSON order is (lon, lat)
    assert route.bbox == pytest.approx((-104.905137, 32.781836, -96.766416, 39.763034))

    body = json.loads(mocked.calls[0].request.body)
    assert body["coordinates"] == [[DALLAS[1], DALLAS[0]], [DENVER[1], DENVER[0]]]
    assert body["instructions"] is False


def test_directions_uses_profile_in_path(mocked, client) -> None:
    mocked.post(HGV_URL, json=load_fixture("directions_dallas_denver.json"))
    client.directions([DALLAS, DENVER], profile="driving-hgv")
    assert mocked.calls[0].request.url == HGV_URL


def test_directions_rejects_unknown_profile(client) -> None:
    with pytest.raises(ValueError, match="profile"):
        client.directions([DALLAS, DENVER], profile="cycling-road")


def test_api_key_sent_in_authorization_header(mocked, client, ors_key) -> None:
    mocked.post(DIRECTIONS_URL, json=load_fixture("directions_dallas_denver.json"))
    client.directions([DALLAS, DENVER])
    request = mocked.calls[0].request
    assert request.headers["Authorization"] == ors_key
    assert ors_key not in request.url


def test_geocode_search_parses_fixture_and_restricts_to_us(mocked, client) -> None:
    mocked.get(SEARCH_URL, json=load_fixture("geocode_search_white_house.json"))

    (result,) = client.geocode("1600 Pennsylvania Ave NW, Washington, DC")

    assert (result.lat, result.lon) == pytest.approx((38.897473, -77.036548))
    assert (result.layer, result.country_a, result.region_a) == ("address", "USA", "DC")
    assert result.label == "1600 Pennsylvania Avenue NW, Washington, DC, USA"
    assert "boundary.country=US" in mocked.calls[0].request.url


def test_geocode_structured_parses_fixture(mocked, client) -> None:
    mocked.get(STRUCTURED_URL, json=load_fixture("geocode_structured_ruther_glen.json"))

    (result,) = client.geocode_structured("Ruther Glen", "VA")

    assert (result.name, result.layer, result.region_a) == ("Ruther Glen", "locality", "VA")
    assert result.county == "Caroline County"
    url = mocked.calls[0].request.url
    assert "locality=Ruther+Glen" in url
    assert "region=VA" in url


def test_geocode_empty_result(mocked, client) -> None:
    mocked.get(SEARCH_URL, json={"type": "FeatureCollection", "features": []})
    assert client.geocode("zzzz") == []


# --- Call counting ------------------------------------------------------------------------


def test_call_counter_separates_calls_from_retry_attempts(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, status=503)
    mocked.post(DIRECTIONS_URL, json=load_fixture("directions_dallas_denver.json"))
    mocked.get(SEARCH_URL, json=load_fixture("geocode_search_white_house.json"))

    with count_external_calls() as counter:
        client.directions([DALLAS, DENVER])
        client.geocode("1600 Pennsylvania Ave NW, Washington, DC")

    assert (counter.calls, counter.attempts) == (2, 3)


def test_call_counter_scopes_are_independent(mocked, client) -> None:
    mocked.get(SEARCH_URL, json={"features": []})
    with count_external_calls() as outer:
        client.geocode("a")
        with count_external_calls() as inner:
            client.geocode("b")
        client.geocode("c")
    assert (outer.calls, inner.calls) == (2, 1)


def test_calls_outside_a_scope_are_not_counted(mocked, client) -> None:
    mocked.get(SEARCH_URL, json={"features": []})
    client.geocode("a")  # must not raise or leak into a later scope
    with count_external_calls() as counter:
        pass
    assert (counter.calls, counter.attempts) == (0, 0)


# --- Retries and error mapping ------------------------------------------------------------


@pytest.mark.parametrize("status", [502, 503, 504])
def test_retries_transient_gateway_errors(mocked, client, status) -> None:
    mocked.post(DIRECTIONS_URL, status=status)
    mocked.post(DIRECTIONS_URL, json=load_fixture("directions_dallas_denver.json"))

    route = client.directions([DALLAS, DENVER])

    assert route.distance_m > 0
    assert len(mocked.calls) == 2


def test_gives_up_after_max_retries_on_503(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, status=503)
    with pytest.raises(RoutingProviderUnavailableError):
        client.directions([DALLAS, DENVER])
    assert len(mocked.calls) == 3  # 1 attempt + 2 retries


def test_connection_error_retried_then_unavailable(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, body=requests.ConnectionError("boom"))
    with count_external_calls() as counter, pytest.raises(RoutingProviderUnavailableError):
        client.directions([DALLAS, DENVER])
    assert (counter.calls, counter.attempts) == (1, 3)


def test_read_timeout_retried_exactly_once_then_succeeds(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, body=requests.ReadTimeout("slow"))
    mocked.post(DIRECTIONS_URL, json=load_fixture("directions_dallas_denver.json"))

    with count_external_calls() as counter:
        route = client.directions([DALLAS, DENVER])

    assert route.distance_m > 0
    assert (counter.calls, counter.attempts) == (1, 2) and len(mocked.calls) == 2


def test_two_read_timeouts_give_504_after_two_attempts(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, body=requests.ReadTimeout("slow"))  # repeats for every call
    with count_external_calls() as counter, pytest.raises(RoutingProviderTimeoutError):
        client.directions([DALLAS, DENVER])
    assert (counter.calls, counter.attempts) == (1, 2) and len(mocked.calls) == 2


def test_geocode_read_timeout_also_retried_once(mocked, client) -> None:
    mocked.get(SEARCH_URL, body=requests.ReadTimeout("slow"))
    mocked.get(SEARCH_URL, json=load_fixture("geocode_search_white_house.json"))
    (result,) = client.geocode("1600 Pennsylvania Ave NW, Washington, DC")
    assert result.region_a == "DC"
    assert len(mocked.calls) == 2


def test_read_timeout_budget_is_separate_from_5xx_budget(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, status=503)
    mocked.post(DIRECTIONS_URL, body=requests.ReadTimeout("slow"))
    mocked.post(DIRECTIONS_URL, status=502)
    mocked.post(DIRECTIONS_URL, json=load_fixture("directions_dallas_denver.json"))
    assert client.directions([DALLAS, DENVER]).distance_m > 0
    assert len(mocked.calls) == 4  # 2 gateway retries + 1 timeout retry


def test_default_timeouts_from_settings(ors_key) -> None:
    client = get_ors_client()
    assert client.connect_timeout == 5.0
    assert client.directions_read_timeout == 30.0
    assert client.read_timeout == 10.0  # geocoding
    assert client.read_timeout_retries == 1


class RecordingSession(requests.Session):
    """Captures the timeout passed to each request (no network)."""

    def __init__(self, payload: dict) -> None:
        super().__init__()
        self.payload = payload
        self.timeouts: list[tuple[float, float]] = []

    def request(self, method, url, **kwargs):  # type: ignore[override]
        self.timeouts.append(kwargs["timeout"])
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(self.payload).encode()
        return response


def test_directions_and_geocode_use_their_own_read_timeouts(ors_key) -> None:
    directions_session = RecordingSession(load_fixture("directions_dallas_denver.json"))
    ORSClient(ors_key, ORS_TEST_BASE_URL, session=directions_session).directions([DALLAS, DENVER])
    geocode_session = RecordingSession(load_fixture("geocode_search_white_house.json"))
    ORSClient(ors_key, ORS_TEST_BASE_URL, session=geocode_session).geocode("White House")

    assert directions_session.timeouts == [(5.0, 30.0)]
    assert geocode_session.timeouts == [(5.0, 10.0)]


def test_connect_timeout_maps_to_unavailable(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, body=requests.ConnectTimeout("no route to host"))
    with pytest.raises(RoutingProviderUnavailableError):
        client.directions([DALLAS, DENVER])


def test_rate_limit_429_not_retried(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, status=429, json={"error": "Rate limit exceeded"})
    with pytest.raises(RateLimitedError) as exc_info:
        client.directions([DALLAS, DENVER])
    assert len(mocked.calls) == 1
    assert exc_info.value.details["provider_status"] == 429


@pytest.mark.parametrize("status", [401, 403])
def test_bad_key_maps_to_unavailable_without_leaking_key(mocked, client, ors_key, status) -> None:
    mocked.post(
        DIRECTIONS_URL, status=status, json={"error": "Access to this API has been disallowed"}
    )
    with pytest.raises(RoutingProviderUnavailableError) as exc_info:
        client.directions([DALLAS, DENVER])
    assert "API key" in exc_info.value.message
    assert ors_key not in repr(exc_info.value.details)


@pytest.mark.parametrize(
    ("ors_code", "status"),
    [(2010, 404), (2009, 404), (2004, 400)],
)
def test_unroutable_trip_maps_to_route_unreachable(mocked, client, ors_code, status) -> None:
    body = {"error": {"code": ors_code, "message": "Could not find routable point"}}
    mocked.post(DIRECTIONS_URL, status=status, json=body)
    with pytest.raises(RouteUnreachableError) as exc_info:
        client.directions([DALLAS, DENVER])
    assert exc_info.value.details["provider_code"] == ors_code


def test_other_provider_error_maps_to_502(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, status=500, json={"error": {"code": 2099, "message": "Unknown"}})
    with pytest.raises(RoutingProviderError) as exc_info:
        client.directions([DALLAS, DENVER])
    assert exc_info.value.status_code == 502


def test_invalid_json_body_maps_to_provider_error(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, body="<html>oops</html>", status=200)
    with pytest.raises(RoutingProviderError):
        client.directions([DALLAS, DENVER])


def test_unexpected_shape_maps_to_provider_error(mocked, client) -> None:
    mocked.post(DIRECTIONS_URL, json={"type": "FeatureCollection", "features": []})
    with pytest.raises(RoutingProviderError):
        client.directions([DALLAS, DENVER])


def test_missing_key_raises_before_any_request(mocked) -> None:
    client = ORSClient("", ORS_TEST_BASE_URL)
    with count_external_calls() as counter, pytest.raises(RoutingNotConfiguredError):
        client.directions([DALLAS, DENVER])
    assert (counter.calls, counter.attempts) == (0, 0)
    assert len(mocked.calls) == 0


def test_get_ors_client_reads_settings(ors_key) -> None:
    client = get_ors_client()
    assert client.configured
    assert client.base_url == ORS_TEST_BASE_URL
    assert get_ors_client() is client  # process-wide singleton
