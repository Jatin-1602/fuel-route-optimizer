"""Route geometry: mile markers and resampling."""

import numpy as np
import pytest

from apps.routing.services.geo import chord_to_miles, haversine_miles, miles_to_chord
from apps.routing.services.route_geometry import build_geometry, resample
from tests.conftest import load_fixture

# One degree of longitude along the equator is ~69.09 miles.
EQUATOR = [(0.0, 0.0), (1.0, 0.0), (3.0, 0.0)]


def test_cumulative_miles_along_polyline() -> None:
    geometry = build_geometry(EQUATOR)
    assert geometry.miles[0] == 0.0
    assert geometry.miles[1] == pytest.approx(69.09, abs=0.05)
    assert geometry.total_miles == pytest.approx(3 * 69.09, abs=0.1)


def test_total_miles_scaled_to_router_distance() -> None:
    geometry = build_geometry(EQUATOR, total_miles=210.0)
    assert geometry.total_miles == pytest.approx(210.0)
    assert geometry.miles[1] == pytest.approx(70.0)  # proportional


def test_resample_spacing_and_endpoints() -> None:
    dense = resample(build_geometry(EQUATOR), spacing_miles=0.5)
    steps = np.diff(dense.miles)
    assert steps.max() <= 0.5 + 1e-9
    assert (dense.lon[0], dense.lat[0]) == (0.0, 0.0)
    assert dense.lon[-1] == pytest.approx(3.0)
    assert dense.total_miles == pytest.approx(build_geometry(EQUATOR).total_miles)


def test_resample_handles_duplicate_vertices() -> None:
    dense = resample(build_geometry([(0, 0), (0, 0), (1, 0)]), spacing_miles=1.0)
    assert np.all(np.diff(dense.miles) > 0)


def test_point_at_interpolates_and_clamps() -> None:
    geometry = build_geometry(EQUATOR, total_miles=300)
    lat, lon = geometry.point_at(50)
    assert (lat, lon) == pytest.approx((0.0, 0.5))
    assert geometry.point_at(-10) == (0.0, 0.0)
    assert geometry.point_at(10_000)[1] == pytest.approx(3.0)


def test_rejects_degenerate_input() -> None:
    with pytest.raises(ValueError):
        build_geometry([(0.0, 0.0)])
    with pytest.raises(ValueError):
        resample(build_geometry(EQUATOR), spacing_miles=0)


def test_real_route_polyline_length_matches_router_distance() -> None:
    """On the recorded NYC->LA route the haversine length is within 1% of ORS's distance."""
    data = load_fixture("directions_nyc_la.json")
    coords = data["features"][0]["geometry"]["coordinates"]
    router_miles = data["features"][0]["properties"]["summary"]["distance"] / 1609.344
    raw = build_geometry([tuple(c) for c in coords])
    assert raw.total_miles == pytest.approx(router_miles, rel=0.01)
    dense = resample(build_geometry([tuple(c) for c in coords], router_miles))
    assert len(dense) == pytest.approx(router_miles / 0.5, abs=2)


def test_unit_vector_chord_roundtrip() -> None:
    assert chord_to_miles(miles_to_chord(123.4)) == pytest.approx(123.4)
    assert haversine_miles(0, 0, 0, 1) == pytest.approx(69.09, abs=0.05)


def test_fixtures_are_geojson_lon_lat_order() -> None:
    data = load_fixture("directions_chicago_houston.json")
    lon, lat = data["features"][0]["geometry"]["coordinates"][0]
    assert -90 < lon < -85 and 41 < lat < 43  # Chicago, given as (lon, lat)
