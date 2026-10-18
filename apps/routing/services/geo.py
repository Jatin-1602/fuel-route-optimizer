"""Spherical-geometry helpers (pure numpy, no Django).

Points are converted to 3D unit vectors so that a KD-tree's Euclidean (chord) distance
is monotonic in great-circle distance — nearest neighbours are correct anywhere on the
globe, with no lat/lon distortion and no antimeridian special cases.
"""

import numpy as np
from numpy.typing import ArrayLike, NDArray

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def to_unit_vectors(lat_deg: ArrayLike, lon_deg: ArrayLike) -> NDArray[np.float64]:
    """Convert latitude/longitude in degrees to an ``(n, 3)`` array of unit vectors."""
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def chord_to_miles(chord: ArrayLike) -> NDArray[np.float64]:
    """Great-circle miles for a unit-sphere chord length."""
    half = np.clip(np.asarray(chord, dtype=np.float64) / 2.0, 0.0, 1.0)
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(half)


def miles_to_chord(miles: float) -> float:
    """Unit-sphere chord length for a great-circle distance in miles."""
    return float(2.0 * np.sin(miles / (2.0 * EARTH_RADIUS_MILES)))


def haversine_miles(
    lat1: ArrayLike, lon1: ArrayLike, lat2: ArrayLike, lon2: ArrayLike
) -> NDArray[np.float64]:
    """Great-circle distance in miles (vectorized, broadcasting)."""
    p1, l1, p2, l2 = (np.radians(np.asarray(v, dtype=np.float64)) for v in (lat1, lon1, lat2, lon2))
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin((l2 - l1) / 2) ** 2
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
