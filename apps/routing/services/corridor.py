"""Find fuel stations within a corridor around a route.

* :class:`StationIndex` — all stations as numpy arrays (prices, coordinates, 3D unit
  vectors), loaded from the database **once per process** (:func:`get_station_index`).
* :func:`find_candidates` — per request: bounding-box prefilter, then a KD-tree built on
  the resampled route points answers "nearest route point" for every remaining station
  in one vectorized query. That nearest point gives the station's mile marker along the
  route and its off-route distance.

Everything except :func:`get_station_index` is pure numpy/scipy (no Django).
"""

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from scipy.spatial import cKDTree

from apps.routing.services.geo import chord_to_miles, miles_to_chord, to_unit_vectors
from apps.routing.services.route_geometry import RouteGeometry

logger = logging.getLogger(__name__)

MILES_PER_DEGREE_LAT = 69.0


@dataclass(frozen=True, slots=True)
class StationRecord:
    """Display fields of a station (kept out of the hot numpy arrays)."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str


@dataclass(frozen=True)
class StationIndex:
    """All stations, column-oriented for vectorized math."""

    records: tuple[StationRecord, ...]
    price: NDArray[np.float64]
    lat: NDArray[np.float64]
    lon: NDArray[np.float64]
    xyz: NDArray[np.float64] = field(repr=False)

    @classmethod
    def from_rows(
        cls, rows: Iterable[tuple[int, str, str, str, str, float, float, float]]
    ) -> "StationIndex":
        """Build from ``(opis_id, name, address, city, state, price, lat, lon)`` rows."""
        rows = list(rows)
        records = tuple(StationRecord(*r[:5]) for r in rows)
        price = np.array([float(r[5]) for r in rows], dtype=np.float64)
        lat = np.array([r[6] for r in rows], dtype=np.float64)
        lon = np.array([r[7] for r in rows], dtype=np.float64)
        xyz = to_unit_vectors(lat, lon) if rows else np.empty((0, 3))
        return cls(records=records, price=price, lat=lat, lon=lon, xyz=xyz)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class Corridor:
    """Stations near a route, sorted by mile marker. Arrays are aligned."""

    station_idx: NDArray[np.int64]  # indices into StationIndex
    mile: NDArray[np.float64]  # distance along the route of the nearest route point
    off_route_miles: NDArray[np.float64]  # straight-line distance to that point
    price: NDArray[np.float64]

    def __len__(self) -> int:
        return len(self.station_idx)


def find_candidates(
    route: RouteGeometry, stations: StationIndex, corridor_miles: float
) -> Corridor:
    """Stations within ``corridor_miles`` of any point of the (resampled) ``route``.

    ``route`` should be resampled densely (<= 1 mile spacing) so that "distance to the
    nearest route point" approximates "distance to the route line" within half a
    spacing.
    """
    if len(stations) == 0:
        empty = np.empty(0)
        return Corridor(empty.astype(np.int64), empty, empty, empty)

    # 1) Cheap prefilter on a padded lat/lon bounding box of the route.
    pad_lat = corridor_miles / MILES_PER_DEGREE_LAT
    max_abs_lat = min(float(np.max(np.abs(route.lat))) + pad_lat, 89.0)
    pad_lon = corridor_miles / (MILES_PER_DEGREE_LAT * np.cos(np.radians(max_abs_lat)))
    mask = (
        (stations.lat >= route.lat.min() - pad_lat)
        & (stations.lat <= route.lat.max() + pad_lat)
        & (stations.lon >= route.lon.min() - pad_lon)
        & (stations.lon <= route.lon.max() + pad_lon)
    )
    pre = np.flatnonzero(mask)

    # 2) Nearest route point for every prefiltered station (one vectorized KD-tree query).
    tree = cKDTree(to_unit_vectors(route.lat, route.lon))
    chord, nearest = tree.query(
        stations.xyz[pre], distance_upper_bound=miles_to_chord(corridor_miles)
    )
    inside = np.isfinite(chord)
    idx = pre[inside]
    mile = route.miles[nearest[inside]]
    off = chord_to_miles(chord[inside])

    order = np.lexsort((off, mile))  # by mile marker, then closest to the road
    return Corridor(
        station_idx=idx[order].astype(np.int64),
        mile=mile[order],
        off_route_miles=off[order],
        price=stations.price[idx[order]],
    )


# --- Process-wide station index -------------------------------------------------------

_index: StationIndex | None = None
_index_lock = threading.Lock()


def get_station_index() -> StationIndex:
    """Stations loaded from the DB on first use, then served from memory (no per-request DB).

    Loaded lazily rather than in ``AppConfig.ready()``: Django warns against queries in
    ``ready()`` (they run during migrate/test setup, before tables may exist).
    """
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = _load_from_db()
                logger.info("Loaded %d fuel stations into memory", len(_index))
    return _index


def reset_station_index() -> None:
    """Drop the cached index so the next request reloads it (e.g. after load_stations)."""
    global _index
    with _index_lock:
        _index = None


def set_station_index(index: StationIndex | None) -> None:
    """Install a specific index (used by tests)."""
    global _index
    with _index_lock:
        _index = index


def _load_from_db() -> StationIndex:
    from apps.stations.models import FuelStation  # Django import kept local: module stays pure

    rows = FuelStation.objects.order_by("opis_id").values_list(
        "opis_id", "name", "address", "city", "state", "price", "lat", "lon"
    )
    return StationIndex.from_rows(rows)
