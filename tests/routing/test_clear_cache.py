"""Cache namespaces and the clear_cache command."""

from io import StringIO

import pytest
import responses
from django.core.cache import cache
from django.core.management import call_command

from apps.routing.services.cache_keys import generation, invalidate, namespaced_key
from apps.routing.services.corridor import StationIndex, set_station_index
from apps.stations.services.station_geocoder import read_geocoded_csv
from tests.conftest import load_fixture
from tests.routing.test_api import CAR_URL, NYC_LA


def test_invalidating_a_namespace_hides_its_entries_only() -> None:
    route_key, geo_key = namespaced_key("route", "a"), namespaced_key("geocode", "a")
    cache.set(route_key, "R")
    cache.set(geo_key, "G")

    invalidate("route")

    assert cache.get(namespaced_key("route", "a")) is None
    assert cache.get(namespaced_key("geocode", "a")) == "G"


def test_unknown_namespace_rejected() -> None:
    with pytest.raises(ValueError):
        namespaced_key("nope", "a")


def test_clear_cache_command_all_namespaces_keeps_throttle_counters() -> None:
    keys = {ns: namespaced_key(ns, "x") for ns in ("route", "response", "geocode")}
    for key in keys.values():
        cache.set(key, 1)
    cache.set("throttle_route_127.0.0.1", [1.0])
    out = StringIO()

    call_command("clear_cache", stdout=out)

    assert all(cache.get(namespaced_key(ns, "x")) is None for ns in keys)
    assert cache.get("throttle_route_127.0.0.1") == [1.0]
    assert "route" in out.getvalue() and "geocode" in out.getvalue()


def test_clear_cache_only_one_namespace() -> None:
    before = generation("geocode")
    call_command("clear_cache", "--only", "route", stdout=StringIO())
    assert generation("geocode") == before


def test_clear_cache_all_wipes_everything() -> None:
    cache.set("throttle_route_127.0.0.1", [1.0])
    call_command("clear_cache", "--all", stdout=StringIO())
    assert cache.get("throttle_route_127.0.0.1") is None


def test_api_is_cold_again_after_clear_cache(api_client, settings) -> None:
    """End to end: after clear_cache the same request recomputes (route call repeated)."""
    settings.ORS_API_KEY = "test-key-not-real"
    rows = [
        (s.opis_id, s.name, s.address, s.city, s.state, float(s.price), s.lat, s.lon)
        for s in read_geocoded_csv(settings.STATIONS_GEOCODED_CSV)
    ]
    set_station_index(StationIndex.from_rows(rows))
    with responses.RequestsMock() as mocked:
        mocked.post(CAR_URL, json=load_fixture("directions_nyc_la.json"))
        first = api_client.get("/api/v1/route/", NYC_LA).json()["meta"]
        cached = api_client.get("/api/v1/route/", NYC_LA).json()["meta"]
        call_command("clear_cache", stdout=StringIO())
        cold = api_client.get("/api/v1/route/", NYC_LA).json()["meta"]

    assert [m["external_api_calls"] for m in (first, cached, cold)] == [1, 0, 1]
    assert [m["cache_hit"] for m in (first, cached, cold)] == [False, True, False]
