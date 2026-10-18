"""Location resolution: coordinates, offline "City, ST", ORS fallback, US validation, caching."""

from collections.abc import Iterator

import pytest
import responses

from apps.routing.exceptions import (
    InvalidInputError,
    LocationNotFoundError,
    LocationOutsideUSAError,
    RoutingNotConfiguredError,
)
from apps.routing.services.geocoding import (
    USGeography,
    get_us_geography,
    parse_city_state,
    parse_coordinates,
    resolve_location,
)
from apps.routing.services.ors_client import count_external_calls
from tests.conftest import ORS_TEST_BASE_URL, load_fixture

SEARCH_URL = f"{ORS_TEST_BASE_URL}/pelias/v1/search"


@pytest.fixture(scope="module")
def geography() -> USGeography:
    """The real offline gazetteer (loaded once for this module)."""
    return get_us_geography()


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


# --- Parsing ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("32.7767,-96.797", (32.7767, -96.797)),
        (" 32.7767 , -96.797 ", (32.7767, -96.797)),
        ("+40,-74", (40.0, -74.0)),
        ("Dallas, TX", None),
        ("1600 Pennsylvania Ave NW, Washington, DC", None),
    ],
)
def test_parse_coordinates(text: str, expected) -> None:
    assert parse_coordinates(text) == expected


@pytest.mark.parametrize("text", ["95,-96", "32,-200"])
def test_parse_coordinates_out_of_range(text: str) -> None:
    with pytest.raises(InvalidInputError):
        parse_coordinates(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Dallas, TX", ("Dallas", "TX")),
        ("dallas, tx", ("dallas", "TX")),
        ("Dallas, Texas", ("Dallas", "TX")),
        ("Dallas TX", ("Dallas", "TX")),
        ("Dallas, TX, USA", ("Dallas", "TX")),
        ("Washington, DC", ("Washington", "DC")),
        ("New York, New York", ("New York", "NY")),
        ("St. Louis, MO, United States", ("St. Louis", "MO")),
        ("Dallas", None),
        ("1600 Pennsylvania Ave NW, Washington, DC", None),
        ("Springfield, XY", None),
    ],
)
def test_parse_city_state(text: str, expected) -> None:
    assert parse_city_state(text) == expected


@pytest.mark.parametrize(
    "text", ["Toronto, ON", "Vancouver, British Columbia", "Tijuana, Mexico", "Calgary, AB, Canada"]
)
def test_foreign_regions_rejected_without_calls(text: str) -> None:
    with pytest.raises(LocationOutsideUSAError):
        parse_city_state(text)


# --- Inside-the-US test -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "lat", "lon"),
    [
        ("NYC", 40.7128, -74.006),
        ("Key West", 24.5551, -81.78),
        ("rural Nevada", 39.5, -116.5),
        ("Big Bend", 29.25, -103.25),
        ("Anchorage", 61.2181, -149.9003),
        ("Honolulu", 21.3069, -157.8583),
        ("Aleutians (east of antimeridian)", 52.88, 173.25),
    ],
)
def test_contains_accepts_us_points(geography, name, lat, lon) -> None:
    assert geography.contains(lat, lon), name


@pytest.mark.parametrize(
    ("name", "lat", "lon"),
    [
        ("Toronto", 43.6532, -79.3832),
        ("Montreal", 45.5019, -73.5674),
        ("Mexico City", 19.4326, -99.1332),
        ("Monterrey", 25.6866, -100.3161),
        ("Atlantic Ocean", 35.0, -72.0),
        ("Gulf of Mexico", 26.0, -90.0),
        ("mid Lake Michigan", 43.5, -87.0),
        ("Bermuda", 32.3078, -64.7505),
        ("London", 51.5072, -0.1276),
    ],
)
def test_contains_rejects_non_us_points(geography, name, lat, lon) -> None:
    assert not geography.contains(lat, lon), name


# --- resolve_location ---------------------------------------------------------------------


def test_coordinates_resolved_offline(geography) -> None:
    with count_external_calls() as calls:
        loc = resolve_location("32.7767,-96.797", geography=geography)
    assert (loc.lat, loc.lon, loc.resolved_by) == (32.7767, -96.797, "coordinates")
    assert calls.count == 0


def test_coordinates_outside_us_rejected(geography) -> None:
    with pytest.raises(LocationOutsideUSAError):
        resolve_location("43.6532,-79.3832", geography=geography)  # Toronto


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("New York, NY", "New York, NY"),
        ("Los Angeles, CA", "Los Angeles, CA"),
        ("Dallas, Texas", "Dallas, TX"),
        ("St Louis, MO", "St. Louis, MO"),
        ("Nashville, TN", "Nashville, TN"),
        ("Anchorage, AK", "Anchorage, AK"),
    ],
)
def test_city_state_resolved_offline_with_zero_calls(geography, query, name) -> None:
    with count_external_calls() as calls:
        loc = resolve_location(query, geography=geography)
    assert (loc.name, loc.resolved_by) == (name, "offline")
    assert calls.count == 0


def test_canadian_city_rejected_with_zero_calls(geography) -> None:
    with count_external_calls() as calls, pytest.raises(LocationOutsideUSAError):
        resolve_location("Toronto, ON", geography=geography)
    assert calls.count == 0


def test_empty_input_rejected(geography) -> None:
    with pytest.raises(InvalidInputError):
        resolve_location("   ", geography=geography)


def test_street_address_uses_one_ors_call_then_cache(geography, mocked, ors_key) -> None:
    mocked.get(SEARCH_URL, json=load_fixture("geocode_search_white_house.json"))
    query = "1600 Pennsylvania Ave NW, Washington, DC"

    with count_external_calls() as first:
        loc = resolve_location(query, geography=geography)
    with count_external_calls() as second:
        again = resolve_location(
            "  1600 pennsylvania ave nw,  washington, dc ", geography=geography
        )

    assert (loc.resolved_by, loc.name) == (
        "ors",
        "1600 Pennsylvania Avenue NW, Washington, DC, USA",
    )
    assert loc.lat == pytest.approx(38.897473)
    assert (first.count, second.count) == (1, 0)  # normalized text hits the cache
    assert (again.lat, again.lon) == (loc.lat, loc.lon)


def test_unknown_city_in_known_state_falls_back_to_ors(geography, mocked, ors_key) -> None:
    mocked.get(SEARCH_URL, json=load_fixture("geocode_structured_ruther_glen.json"))
    with count_external_calls() as calls:
        loc = resolve_location("Ruther Glen, VA", geography=geography)
    assert (loc.resolved_by, loc.name) == ("ors", "Ruther Glen, VA, USA")
    assert calls.count == 1


def test_ors_result_in_wrong_state_is_rejected(geography, mocked, ors_key) -> None:
    mocked.get(SEARCH_URL, json=load_fixture("geocode_structured_ruther_glen.json"))  # a VA result
    with pytest.raises(LocationNotFoundError):
        resolve_location("Ruther Glen, MD", geography=geography)


def test_ors_no_result_is_location_not_found(geography, mocked, ors_key) -> None:
    mocked.get(SEARCH_URL, json={"type": "FeatureCollection", "features": []})
    with pytest.raises(LocationNotFoundError):
        resolve_location("123 Nowhere Lane, Faketown", geography=geography)


def test_ors_non_us_result_is_ignored(geography, mocked, ors_key) -> None:
    feature = load_fixture("geocode_search_white_house.json")
    feature["features"][0]["properties"]["country_a"] = "CAN"
    mocked.get(SEARCH_URL, json=feature)
    with pytest.raises(LocationNotFoundError):
        resolve_location("1600 Pennsylvania Ave NW, Washington, DC", geography=geography)


def test_free_text_without_key_reports_not_configured(geography) -> None:
    with pytest.raises(RoutingNotConfiguredError):
        resolve_location("1600 Pennsylvania Ave NW, Washington, DC", geography=geography)


def test_geography_singleton_is_reused() -> None:
    assert get_us_geography() is get_us_geography()
