"""Offline station geocoding and its CSV outputs."""

from decimal import Decimal
from pathlib import Path

from django.conf import settings

from apps.stations.services.cleaning import CleanStation
from apps.stations.services.gazetteer import Place, PlaceIndex, PlaceKind
from apps.stations.services.station_geocoder import (
    geocode_stations,
    read_geocoded_csv,
    write_geocoded_csv,
)
from apps.stations.us_states import US_STATE_CODES


def _station(opis_id: int, city: str, state: str) -> CleanStation:
    return CleanStation(opis_id, f"STOP {opis_id}", "I-1", city, state, 1, Decimal("3.12345678"))


def _index() -> PlaceIndex:
    return PlaceIndex(
        [
            Place("TX", "dallas", "Dallas", 32.79, -96.77, PlaceKind.INCORPORATED),
            Place("MI", "redford", "Redford", 42.41, -83.29, PlaceKind.COUSUB_CIVIL),
        ]
    )


def test_geocode_assigns_coordinates_and_source() -> None:
    geocoded, report = geocode_stations(
        [_station(1, "Dallas", "TX"), _station(2, "Redford", "MI")], _index()
    )
    assert [(g.opis_id, g.lat, g.geocode_source, g.geocode_match) for g in geocoded] == [
        (1, 32.79, "census_place", "exact"),
        (2, 42.41, "census_cousub", "exact"),
    ]
    assert report.match_rate == 1.0


def test_unmatched_cities_are_grouped_and_reported() -> None:
    stations = [
        _station(1, "Dallas", "TX"),
        _station(2, "Antioch", "TN"),
        _station(3, "Antioch", "TN"),
    ]
    geocoded, report = geocode_stations(stations, _index())

    assert len(geocoded) == 1
    assert report.unmatched_stations == 2
    assert [(c.city, c.state, c.station_count) for c in report.unmatched_cities] == [
        ("Antioch", "TN", 2)
    ]
    assert report.match_rate == 1 / 3


def test_geocoded_csv_roundtrip_preserves_exact_price(tmp_path: Path) -> None:
    geocoded, _ = geocode_stations([_station(1, "Dallas", "TX")], _index())
    path = tmp_path / "out.csv"
    write_geocoded_csv(geocoded, path)
    assert read_geocoded_csv(path) == geocoded


def test_committed_geocoded_csv_is_consistent() -> None:
    stations = read_geocoded_csv(settings.STATIONS_GEOCODED_CSV)

    assert len(stations) > 6000
    assert len({s.opis_id for s in stations}) == len(stations)
    assert {s.state for s in stations} <= US_STATE_CODES
    assert all(s.price > 0 for s in stations)
    # Contiguous US bounding box (no AK/HI stations in the source data).
    assert all(24 < s.lat < 50 and -125 < s.lon < -66 for s in stations)
