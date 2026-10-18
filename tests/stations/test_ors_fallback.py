"""ORS fallback for unmatched station cities (no network: a fake client is used)."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from apps.routing.exceptions import RateLimitedError, RoutingProviderError
from apps.routing.services.ors_client import GeocodeResult
from apps.stations.services.cleaning import CleanStation
from apps.stations.services.gazetteer import Place, PlaceIndex, PlaceKind, StateBounds
from apps.stations.services.ors_fallback import (
    RESULTS_PER_QUERY,
    FallbackCache,
    decide_all,
    evaluate,
    fetch_missing,
)
from apps.stations.services.station_geocoder import find_unmatched_pairs, geocode_stations

BOUNDS = {
    "VA": StateBounds("VA", 36.3, 39.6, -83.7, -75.1),
    "TN": StateBounds("TN", 34.7, 36.9, -90.5, -81.4),
    "MI": StateBounds("MI", 41.5, 48.5, -90.6, -82.2),
}
COUNTIES = frozenset({("VA", "henrico"), ("VA", "chesterfield")})

RUTHER_GLEN = GeocodeResult(
    lat=37.92875,
    lon=-77.45582,
    label="Ruther Glen, VA, USA",
    name="Ruther Glen",
    layer="locality",
    country_a="USA",
    region_a="VA",
    confidence=1.0,
    match_type="exact",
    source="whosonfirst",
    county="Caroline County",
)


def _antioch(lat: float, lon: float) -> GeocodeResult:
    return replace(
        RUTHER_GLEN, name="Antioch", label="Antioch, TN, USA", region_a="TN", lat=lat, lon=lon
    )


class FakeClient:
    """Records calls; returns canned answers or raises a queued exception."""

    def __init__(self, answers: dict[str, list[GeocodeResult] | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, int]] = []

    def geocode_structured(self, locality: str, region: str, size: int = 1) -> list[GeocodeResult]:
        self.calls.append((locality, region, size))
        answer = self.answers.get(f"{locality}|{region}", [])
        if isinstance(answer, Exception):
            raise answer
        return answer


# --- rules 1-4 ----------------------------------------------------------------------------


def test_accepts_locality_in_same_state_inside_bounds() -> None:
    decision = evaluate("Ruther Glen", "VA", [RUTHER_GLEN], BOUNDS)
    assert (decision.accepted, decision.reason, decision.result) == (True, "accepted", RUTHER_GLEN)


def test_accepts_localadmin_layer() -> None:
    assert evaluate(
        "Ruther Glen", "VA", [replace(RUTHER_GLEN, layer="localadmin")], BOUNDS
    ).accepted


@pytest.mark.parametrize(
    ("change", "reason_fragment"),
    [
        ({"layer": "region"}, "layer 'region' is not locality-type"),
        ({"layer": "neighbourhood"}, "not locality-type"),
        ({"layer": "county"}, "not locality-type"),
        ({"country_a": "CAN"}, "country 'CAN' is not USA"),
        ({"region_a": "MD"}, "state 'MD' != CSV state 'VA'"),
        ({"lat": 40.5}, "outside VA bounding box"),
        ({"lon": -70.0}, "outside VA bounding box"),
    ],
)
def test_basic_rejections_carry_a_reason(change: dict, reason_fragment: str) -> None:
    decision = evaluate("Ruther Glen", "VA", [replace(RUTHER_GLEN, **change)], BOUNDS)
    assert not decision.accepted
    assert reason_fragment in decision.reason


def test_no_result_is_rejected() -> None:
    decision = evaluate("Nowhere", "VA", [], BOUNDS)
    assert (decision.accepted, decision.reason, decision.result) == (False, "no result", None)


def test_unknown_state_bounds_rejected() -> None:
    assert not evaluate("Ruther Glen", "VA", [RUTHER_GLEN], {}).accepted


def test_later_candidate_used_when_top_result_fails_rules() -> None:
    region = replace(RUTHER_GLEN, layer="region", name="Virginia", lat=37.5, lon=-78.8)
    decision = evaluate("Ruther Glen", "VA", [region, RUTHER_GLEN], BOUNDS)
    assert decision.accepted
    assert decision.result == RUTHER_GLEN


# --- rule 5: ambiguity --------------------------------------------------------------------


def test_same_name_localities_far_apart_are_ambiguous() -> None:
    results = [_antioch(35.68, -88.44), _antioch(36.06, -86.67), _antioch(36.36, -86.26)]
    decision = evaluate("Antioch", "TN", results, BOUNDS)
    assert not decision.accepted
    assert decision.reason.startswith("ambiguous: 3 localities")


def test_same_name_duplicates_close_together_are_not_ambiguous() -> None:
    results = [_antioch(36.06, -86.67), _antioch(36.07, -86.68)]  # same place, two sources
    assert evaluate("Antioch", "TN", results, BOUNDS).accepted


def test_exact_name_preferred_over_higher_ranked_partial_name() -> None:
    afb = replace(
        RUTHER_GLEN, name="K. I. Sawyer Air Force Base", region_a="MI", lat=46.35, lon=-87.39
    )
    sawyer = replace(RUTHER_GLEN, name="Sawyer", region_a="MI", lat=41.89, lon=-86.59)
    decision = evaluate("Sawyer", "MI", [afb, sawyer], BOUNDS)
    assert decision.accepted
    assert decision.result == sawyer


def test_name_variant_accepted_when_no_exact_name_exists() -> None:
    variant = replace(RUTHER_GLEN, name="Ellabelle", label="Ellabelle, VA, USA")
    assert evaluate("Ellabell", "VA", [variant], BOUNDS).accepted


# --- rule 6: county collision -------------------------------------------------------------


def test_county_named_city_must_resolve_inside_that_county() -> None:
    hamlet = replace(
        RUTHER_GLEN, name="Henrico", lat=36.59, lon=-78.62, county="Northampton County"
    )
    decision = evaluate("Henrico", "VA", [hamlet], BOUNDS, COUNTIES)
    assert not decision.accepted
    assert "also a county" in decision.reason


def test_county_named_city_inside_its_county_is_accepted() -> None:
    seat = replace(
        RUTHER_GLEN,
        name="Chesterfield Court House",
        lat=37.38,
        lon=-77.5,
        county="Chesterfield County",
    )
    assert evaluate("Chesterfield", "VA", [seat], BOUNDS, COUNTIES).accepted


# --- cache + fetching -------------------------------------------------------------------


def test_fetch_queries_each_uncached_pair_once_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    client = FakeClient({"Ruther Glen|VA": [RUTHER_GLEN]})
    pairs = [("Ruther Glen", "VA"), ("Nowhere", "VA")]

    calls = fetch_missing(pairs, FallbackCache(path), client, 0, sleep=lambda _: None)
    again = fetch_missing(pairs, FallbackCache(path), client, 0, sleep=lambda _: None)

    assert (calls, again) == (2, 0)
    assert client.calls == [(c, s, RESULTS_PER_QUERY) for c, s in pairs]
    reloaded = FallbackCache(path)
    assert reloaded.get("Ruther Glen|VA") == [RUTHER_GLEN]
    assert reloaded.get("Nowhere|VA") == []  # empty answers are cached too


def test_fetch_is_rate_limited(tmp_path: Path) -> None:
    sleeps: list[float] = []
    pairs = [("A", "VA"), ("B", "VA"), ("C", "VA")]

    fetch_missing(
        pairs, FallbackCache(tmp_path / "c.json"), FakeClient({}), 0.7, sleep=sleeps.append
    )

    assert len(sleeps) == 2  # no wait before the first call
    assert all(0 < s <= 0.7 for s in sleeps)


def test_transient_error_skips_pair_without_caching(tmp_path: Path) -> None:
    cache = FallbackCache(tmp_path / "c.json")
    client = FakeClient({"A|VA": RoutingProviderError(), "B|VA": [RUTHER_GLEN]})

    fetch_missing([("A", "VA"), ("B", "VA")], cache, client, 0, sleep=lambda _: None)

    assert "A|VA" not in cache
    assert "B|VA" in cache


def test_quota_error_stops_batch_but_keeps_progress(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    client = FakeClient({"A|VA": [RUTHER_GLEN], "B|VA": RateLimitedError()})

    with pytest.raises(RateLimitedError):
        fetch_missing([("A", "VA"), ("B", "VA"), ("C", "VA")], FallbackCache(path), client, 0)

    assert [c[:2] for c in client.calls] == [("A", "VA"), ("B", "VA")]
    assert "A|VA" in FallbackCache(path)


def test_cache_rejects_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text('{"version": 1, "entries": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        FallbackCache(path)


# --- integration with station geocoding --------------------------------------------------


def test_accepted_fallback_supplies_ors_coordinates_and_rejections_are_reported(
    tmp_path: Path,
) -> None:
    index = PlaceIndex([Place("VA", "richmond", "Richmond", 37.5, -77.4, PlaceKind.INCORPORATED)])
    stations = [
        CleanStation(1, "A", "I-95", "Richmond", "VA", 1, Decimal("3.1")),
        CleanStation(2, "B", "I-95", "Ruther Glen", "VA", 1, Decimal("3.2")),
        CleanStation(3, "C", "I-95", "Henrico", "VA", 1, Decimal("3.3")),
    ]
    hamlet = replace(
        RUTHER_GLEN, name="Henrico", label="Henrico, VA, USA", county="Northampton County"
    )
    cache = FallbackCache(tmp_path / "c.json")
    cache.put("Ruther Glen|VA", [RUTHER_GLEN])
    cache.put("Henrico|VA", [hamlet])

    pairs = find_unmatched_pairs(stations, index)
    decisions = {(d.city, d.state): d for d in decide_all(pairs, cache, BOUNDS, COUNTIES)}
    geocoded, report = geocode_stations(stations, index, decisions)

    assert pairs == [("Henrico", "VA"), ("Ruther Glen", "VA")]
    ors_rows = [g for g in geocoded if g.geocode_source == "ors"]
    assert [(g.opis_id, g.lat, g.geocode_match) for g in ors_rows] == [(2, 37.92875, "exact")]
    assert report.matched_by["ors:exact"] == 1
    (henrico,) = report.unmatched_cities
    assert henrico.ors_result == "Henrico, VA, USA"
    assert "also a county" in henrico.ors_reason
