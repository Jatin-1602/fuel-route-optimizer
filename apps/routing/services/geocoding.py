"""Resolve user-supplied start/finish strings to US coordinates.

Resolution order (and its external-call cost):

1. ``"lat,lon"``                      -> parsed and checked to be inside the US     [0 calls]
2. ``"City, ST"`` / ``"City, State"`` -> offline Census gazetteer lookup              [0 calls]
3. anything else (street addresses), or a city the gazetteer does not know
                                      -> ORS geocode (US only), cached               [1 call]
"""

import hashlib
import logging
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from django.conf import settings
from django.core.cache import cache
from scipy.spatial import cKDTree

from apps.routing.exceptions import (
    InvalidInputError,
    LocationNotFoundError,
    LocationOutsideUSAError,
)
from apps.routing.services.geo import chord_to_miles, to_unit_vectors
from apps.routing.services.ors_client import GeocodeResult, ORSClient, get_ors_client
from apps.stations.services.gazetteer import (
    Place,
    PlaceIndex,
    StateBounds,
    compute_state_bounds,
    read_places_csv,
)
from apps.stations.us_states import STATE_NAME_TO_CODE, US_STATE_CODES

logger = logging.getLogger(__name__)

ResolvedBy = Literal["coordinates", "offline", "ors"]

# "Inside the US" test for raw coordinates: a point must be within this many miles of a
# Census place, or within LOCAL_SPACING_FACTOR x that place's own neighbour spacing
# (so sparse areas like rural Nevada pass while Toronto/Lake Michigan/the Gulf do not).
MIN_ACCEPT_MILES = 10.0
LOCAL_SPACING_FACTOR = 2.5
_SPACING_NEIGHBOUR = 3  # spacing = distance to the 3rd-nearest other place

_FOREIGN_REGIONS: frozenset[str] = frozenset(
    {
        # Canadian provinces / territories (codes and names)
        "ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt",
        "alberta", "british columbia", "manitoba", "new brunswick", "newfoundland",
        "newfoundland and labrador", "nova scotia", "northwest territories", "nunavut",
        "ontario", "prince edward island", "quebec", "québec", "saskatchewan", "yukon",
        # Neighbouring countries
        "canada", "mexico", "méxico",
    }
)  # fmt: skip
_US_SUFFIXES: frozenset[str] = frozenset(
    {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
)
_COORDINATES_RE = re.compile(r"^\s*([+-]?\d{1,3}(?:\.\d+)?)\s*,\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$")
_CITY_SPACE_STATE_RE = re.compile(r"^(?P<city>.*\S)\s+(?P<state>[A-Za-z]{2})$")


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    """A user input resolved to a point."""

    query: str
    name: str
    lat: float
    lon: float
    resolved_by: ResolvedBy


# --- Offline US geography ---------------------------------------------------------------


class USGeography:
    """Offline gazetteer index + state boxes + KD-tree for "is this point in the US?"."""

    def __init__(self, places: list[Place]) -> None:
        self.index = PlaceIndex(places)
        self.bounds: dict[str, StateBounds] = compute_state_bounds(places)
        self._places = places
        self._tree = cKDTree(to_unit_vectors([p.lat for p in places], [p.lon for p in places]))
        chords, _ = self._tree.query(self._tree.data, k=_SPACING_NEIGHBOUR + 1)
        self._spacing_miles = chord_to_miles(chords[:, _SPACING_NEIGHBOUR])

    @classmethod
    def from_csv(cls, path: Path) -> "USGeography":
        """Build from ``data/us_places.csv``."""
        return cls(read_places_csv(path))

    def nearest_place(self, lat: float, lon: float) -> tuple[Place, float]:
        """Closest gazetteer place and its distance in miles."""
        chord, i = self._tree.query(to_unit_vectors([lat], [lon])[0])
        return self._places[int(i)], float(chord_to_miles(chord))

    def contains(self, lat: float, lon: float) -> bool:
        """Whether a point is (very likely) inside the 50 states + DC.

        Limitation: points within ~10 miles of the land border (e.g. Windsor, ON or
        Tijuana) can pass; points in Canada proper, Mexico, or open water do not.
        """
        if not any(b.contains(lat, lon) for b in self.bounds.values()):
            return False
        chord, i = self._tree.query(to_unit_vectors([lat], [lon])[0])
        distance = float(chord_to_miles(chord))
        allowed = max(MIN_ACCEPT_MILES, LOCAL_SPACING_FACTOR * float(self._spacing_miles[int(i)]))
        return distance <= allowed


_geography: USGeography | None = None
_geography_lock = threading.Lock()


def get_us_geography() -> USGeography:
    """Process-wide :class:`USGeography`, loaded lazily on first use (~1 s, then cached).

    Loaded on first request rather than in ``AppConfig.ready()`` so management commands
    and migrations do not pay for it.
    """
    global _geography
    if _geography is None:
        with _geography_lock:
            if _geography is None:
                _geography = USGeography.from_csv(settings.US_PLACES_CSV)
                logger.info("Loaded offline gazetteer: %d places", len(_geography.index))
    return _geography


# --- Parsing ------------------------------------------------------------------------------


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Parse ``"lat,lon"``; returns ``None`` if the text is not in that form."""
    match = _COORDINATES_RE.match(text)
    if not match:
        return None
    lat, lon = float(match[1]), float(match[2])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise InvalidInputError(
            "Coordinates must be 'lat,lon' with -90<=lat<=90 and -180<=lon<=180.",
            value=text,
        )
    return lat, lon


def parse_city_state(text: str) -> tuple[str, str] | None:
    """Parse ``"City, ST"``, ``"City, State"``, ``"City ST"`` (optional trailing ", USA").

    Returns ``(city, state_code)``; ``None`` if the text does not end in a US state.
    Raises :class:`LocationOutsideUSAError` for e.g. ``"Toronto, ON"`` or ``"..., Mexico"``.
    """
    parts = [p.strip() for p in text.split(",") if p.strip()]
    while len(parts) > 1 and parts[-1].lower() in _US_SUFFIXES:
        parts.pop()
    if parts and parts[-1].lower() in _FOREIGN_REGIONS:
        raise LocationOutsideUSAError(f"'{text}' is not in the United States.", query=text)

    if len(parts) == 2:
        city, region = parts
    elif len(parts) == 1 and (m := _CITY_SPACE_STATE_RE.match(parts[0])):
        city, region = m["city"], m["state"]
        if region.upper() not in US_STATE_CODES:
            return None
    else:
        return None

    code = (
        region.upper()
        if region.upper() in US_STATE_CODES
        else STATE_NAME_TO_CODE.get(region.lower())
    )
    return (city, code) if code else None


# --- Resolution ---------------------------------------------------------------------------


def _cache_key(text: str) -> str:
    digest = hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:32]
    return f"geocode:v1:{digest}"


def geocode_cached(text: str, client: ORSClient) -> list[GeocodeResult]:
    """ORS free-text geocode with a Django-cache layer (TTL ``GEOCODE_CACHE_TTL_SECONDS``)."""
    key = _cache_key(text)
    cached = cache.get(key)
    if cached is not None:
        logger.info("Geocode cache hit for %r", text)
        return [GeocodeResult(**item) for item in cached]
    results = client.geocode(text, size=5)
    cache.set(key, [asdict(r) for r in results], settings.GEOCODE_CACHE_TTL_SECONDS)
    return results


def resolve_location(
    query: str,
    *,
    geography: USGeography | None = None,
    client: ORSClient | None = None,
) -> ResolvedLocation:
    """Resolve one start/finish string. See the module docstring for the order and cost."""
    text = " ".join((query or "").split())
    if not text:
        raise InvalidInputError("Location must not be empty.")
    geography = geography or get_us_geography()

    coordinates = parse_coordinates(text)
    if coordinates is not None:
        lat, lon = coordinates
        if not geography.contains(lat, lon):
            raise LocationOutsideUSAError(
                f"Coordinates {lat},{lon} are outside the United States.", query=text
            )
        return ResolvedLocation(text, f"{lat:.5f},{lon:.5f}", lat, lon, "coordinates")

    city_state = parse_city_state(text)
    if city_state is not None:
        city, state = city_state
        match = geography.index.lookup(city, state)
        if match is not None:
            place = match.place
            name = f"{place.display_name}, {state}"
            return ResolvedLocation(text, name, place.lat, place.lon, "offline")
        logger.info("%r not in offline gazetteer; falling back to ORS geocoding", text)

    return _resolve_with_ors(text, city_state, geography, client or get_ors_client())


def _resolve_with_ors(
    text: str,
    city_state: tuple[str, str] | None,
    geography: USGeography,
    client: ORSClient,
) -> ResolvedLocation:
    results = geocode_cached(text, client)
    expected_state = city_state[1] if city_state else None
    for result in results:
        if result.country_a != "USA" or not geography.contains(result.lat, result.lon):
            continue
        if expected_state and result.region_a and result.region_a != expected_state:
            continue
        return ResolvedLocation(text, result.label or text, result.lat, result.lon, "ors")
    if results:
        raise LocationNotFoundError(
            f"No location in the United States matched '{text}'.",
            query=text,
            closest=results[0].label,
        )
    raise LocationNotFoundError(f"Location '{text}' was not found.", query=text)
