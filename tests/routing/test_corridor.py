"""Corridor search: which stations are near a route, and where along it."""

import numpy as np
import pytest

from apps.routing.services.corridor import (
    StationIndex,
    find_candidates,
    get_station_index,
    reset_station_index,
)
from apps.routing.services.route_geometry import build_geometry, resample

MILES_PER_DEG = 69.09  # at the equator


def station(opis_id: int, lat: float, lon: float, price: float = 3.0):
    return (opis_id, f"S{opis_id}", "I-1", "Town", "TX", price, lat, lon)


@pytest.fixture
def equator_route():
    """A 3-degree (~207 mi) route along the equator, resampled every 0.5 mi."""
    return resample(build_geometry([(0.0, 0.0), (3.0, 0.0)]))


def test_finds_stations_within_corridor_with_mile_marker_and_offset(equator_route) -> None:
    index = StationIndex.from_rows(
        [
            station(1, 5 / MILES_PER_DEG, 1.0),  # 5 mi north of mile ~69
            station(2, -9 / MILES_PER_DEG, 2.0),  # 9 mi south of mile ~138
            station(3, 15 / MILES_PER_DEG, 1.5),  # 15 mi off: outside a 10-mi corridor
            station(4, 0.0, 5.0),  # beyond the route's end
        ]
    )
    corridor = find_candidates(equator_route, index, corridor_miles=10)

    assert [index.records[i].opis_id for i in corridor.station_idx] == [1, 2]
    assert corridor.mile == pytest.approx([69.09, 138.18], abs=0.5)
    assert corridor.off_route_miles == pytest.approx([5, 9], abs=0.05)
    assert corridor.price.tolist() == [3.0, 3.0]


def test_sorted_by_mile_then_offset(equator_route) -> None:
    index = StationIndex.from_rows(
        [station(1, 2 / MILES_PER_DEG, 2.0), station(2, 1 / MILES_PER_DEG, 2.0), station(3, 0, 0.5)]
    )
    corridor = find_candidates(equator_route, index, corridor_miles=10)
    assert [index.records[i].opis_id for i in corridor.station_idx] == [3, 2, 1]


def test_wider_corridor_includes_more(equator_route) -> None:
    index = StationIndex.from_rows([station(1, 15 / MILES_PER_DEG, 1.5)])
    assert len(find_candidates(equator_route, index, 10)) == 0
    assert len(find_candidates(equator_route, index, 20)) == 1


def test_station_before_start_snaps_to_mile_zero(equator_route) -> None:
    index = StationIndex.from_rows([station(1, 0.0, -3 / MILES_PER_DEG)])
    corridor = find_candidates(equator_route, index, 10)
    assert corridor.mile.tolist() == [0.0]
    assert corridor.off_route_miles[0] == pytest.approx(3, abs=0.05)


def test_empty_station_index(equator_route) -> None:
    corridor = find_candidates(equator_route, StationIndex.from_rows([]), 10)
    assert len(corridor) == 0


@pytest.mark.django_db
def test_get_station_index_loads_from_db_once_and_resets() -> None:
    from apps.stations.models import FuelStation

    FuelStation.objects.create(
        opis_id=7, name="A", city="Dallas", state="TX", price="3.1", lat=32.8, lon=-96.8,
        geocode_source="census_place", geocode_match="exact",
    )  # fmt: skip
    first = get_station_index()
    FuelStation.objects.create(
        opis_id=8, name="B", city="Austin", state="TX", price="3.2", lat=30.3, lon=-97.7,
        geocode_source="census_place", geocode_match="exact",
    )  # fmt: skip
    assert get_station_index() is first and len(first) == 1  # served from memory
    reset_station_index()
    reloaded = get_station_index()
    assert len(reloaded) == 2
    assert reloaded.price.dtype == np.float64
    assert reloaded.records[0].opis_id == 7
