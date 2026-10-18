"""Route polyline -> mile markers (pure numpy, no Django).

The routed polyline has very uneven vertex spacing (dense in cities, sparse on straight
interstates). We compute cumulative great-circle miles along it and resample it at a
uniform spacing so the corridor search has an evenly dense set of points to match
stations against.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from apps.routing.services.geo import haversine_miles

DEFAULT_SPACING_MILES = 0.5


@dataclass(frozen=True)
class RouteGeometry:
    """A polyline with a mile marker for every point (``miles[0] == 0``)."""

    lat: NDArray[np.float64]
    lon: NDArray[np.float64]
    miles: NDArray[np.float64]

    @property
    def total_miles(self) -> float:
        """Length of the route in miles."""
        return float(self.miles[-1])

    def __len__(self) -> int:
        return len(self.miles)

    def point_at(self, mile: float) -> tuple[float, float]:
        """Interpolated ``(lat, lon)`` at a mile marker (clamped to the route)."""
        mile = min(max(mile, 0.0), self.total_miles)
        return float(np.interp(mile, self.miles, self.lat)), float(
            np.interp(mile, self.miles, self.lon)
        )


def build_geometry(
    lon_lat: Sequence[tuple[float, float]], total_miles: float | None = None
) -> RouteGeometry:
    """Cumulative miles along a GeoJSON-ordered ``(lon, lat)`` polyline.

    If ``total_miles`` (the router's road distance) is given, the haversine distances
    are scaled to it so mile markers agree with the distance we report. The scale is
    ~1.00 because the polyline follows the road; it only absorbs rounding.
    """
    coords = np.asarray(lon_lat, dtype=np.float64)
    if coords.ndim != 2 or coords.shape[0] < 2:
        raise ValueError("A route needs at least two points.")
    lon, lat = coords[:, 0], coords[:, 1]
    steps = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    miles = np.concatenate(([0.0], np.cumsum(steps)))
    if total_miles is not None and miles[-1] > 0:
        miles *= total_miles / miles[-1]
    return RouteGeometry(lat=lat, lon=lon, miles=miles)


def resample(
    geometry: RouteGeometry, spacing_miles: float = DEFAULT_SPACING_MILES
) -> RouteGeometry:
    """Points every ``spacing_miles`` along the route (plus the exact end point).

    Linear interpolation of lat/lon between original vertices is accurate here because
    consecutive vertices are at most a few miles apart.
    """
    if spacing_miles <= 0:
        raise ValueError("spacing_miles must be positive")
    total = geometry.total_miles
    targets = np.arange(0.0, total, spacing_miles)
    targets = np.append(targets, total)
    # Drop zero-length steps (duplicate vertices) so np.interp gets increasing x values.
    keep = np.concatenate(([True], np.diff(geometry.miles) > 0))
    x = geometry.miles[keep]
    return RouteGeometry(
        lat=np.interp(targets, x, geometry.lat[keep]),
        lon=np.interp(targets, x, geometry.lon[keep]),
        miles=targets,
    )
