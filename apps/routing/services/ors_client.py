"""OpenRouteService (HeiGIT) HTTP client: directions + Pelias geocoding.

* One shared ``requests.Session`` (connection pooling / keep-alive).
* Connect timeout 5 s; read timeout 30 s for directions (ORS's first computation of a
  long route can take 10-20 s) and 10 s for geocoding. Retries: up to 2 for connection
  errors and 502/503/504, and exactly one on a read timeout (both requests are
  idempotent).
* Calls (API operations) and HTTP attempts (incl. retries) are counted in the active
  :func:`count_external_calls` scope; the API reports them as ``meta.external_api_calls``
  and ``meta.external_api_attempts``.
* Provider failures are mapped to domain exceptions (``apps.routing.exceptions``).
* The API key is sent in the ``Authorization`` header and is never logged.
"""

import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import requests
from django.conf import settings

from apps.routing.exceptions import (
    RateLimitedError,
    RouteUnreachableError,
    RoutingNotConfiguredError,
    RoutingProviderError,
    RoutingProviderTimeoutError,
    RoutingProviderUnavailableError,
)

logger = logging.getLogger(__name__)

DIRECTIONS_PATH = "/openrouteservice/v2/directions/{profile}/geojson"
GEOCODE_SEARCH_PATH = "/pelias/v1/search"
GEOCODE_STRUCTURED_PATH = "/pelias/v1/search/structured"

PROFILES: tuple[str, ...] = ("driving-car", "driving-hgv")
# How far (meters) ORS may snap an input point to the road network. City centroids can
# sit in parks or rivers; the default (350 m) is too strict for them.
SNAP_RADIUS_METERS = 5000
# ORS error codes meaning "this trip cannot be routed" rather than "the provider broke":
# 2004 = a limit was exceeded (e.g. > 6,000 km), 2009 = route not found,
# 2010 = no routable point near a coordinate.
ROUTE_NOT_POSSIBLE_CODES: frozenset[int] = frozenset({2004, 2009, 2010})
_RETRYABLE_STATUS: frozenset[int] = frozenset({502, 503, 504})


# --- External call counting -------------------------------------------------------------


@dataclass
class ExternalCallCounter:
    """Tally of external API usage within one scope.

    ``calls`` counts API operations (one directions or geocode request), which is what
    the assignment's "one call is ideal" refers to; ``attempts`` counts HTTP requests
    including retries, so a retried timeout stays visible.
    """

    calls: int = 0
    attempts: int = 0


_active_counter: ContextVar[ExternalCallCounter | None] = ContextVar(
    "ors_call_counter", default=None
)


@contextmanager
def count_external_calls() -> Iterator[ExternalCallCounter]:
    """Count ORS calls and HTTP attempts made inside this ``with`` block (request-scoped)."""
    counter = ExternalCallCounter()
    token = _active_counter.set(counter)
    try:
        yield counter
    finally:
        _active_counter.reset(token)


def _record_call() -> None:
    counter = _active_counter.get()
    if counter is not None:
        counter.calls += 1


def _record_attempt() -> None:
    counter = _active_counter.get()
    if counter is not None:
        counter.attempts += 1


# --- Results ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Route:
    """A routed path. ``coordinates`` are ``(lon, lat)`` pairs exactly as GeoJSON orders them."""

    coordinates: list[tuple[float, float]]
    distance_m: float
    duration_s: float
    bbox: tuple[float, float, float, float] | None = None  # (min_lon, min_lat, max_lon, max_lat)


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    """One Pelias feature, reduced to what we use."""

    lat: float
    lon: float
    label: str
    name: str
    layer: str  # "address", "locality", "localadmin", "neighbourhood", "region", ...
    country_a: str  # ISO alpha-3, e.g. "USA"
    region_a: str  # state code, e.g. "VA"
    confidence: float
    match_type: str  # "exact", "interpolated", "fallback", ...
    source: str
    county: str = ""  # e.g. "Caroline County"

    @classmethod
    def from_feature(cls, feature: dict[str, Any]) -> "GeocodeResult":
        """Build from a GeoJSON feature in a Pelias response."""
        lon, lat = feature["geometry"]["coordinates"][:2]
        props = feature.get("properties", {})
        return cls(
            lat=float(lat),
            lon=float(lon),
            label=props.get("label", ""),
            name=props.get("name", ""),
            layer=props.get("layer", ""),
            country_a=props.get("country_a", ""),
            region_a=props.get("region_a", ""),
            confidence=float(props.get("confidence", 0.0)),
            match_type=props.get("match_type", ""),
            source=props.get("source", ""),
            county=props.get("county", ""),
        )


# --- Client -------------------------------------------------------------------------------


class ORSClient:
    """Thin, typed wrapper around the HeiGIT OpenRouteService endpoints we need."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        directions_read_timeout: float = 30.0,
        max_retries: int = 2,
        read_timeout_retries: int = 1,
        backoff_seconds: float = 0.25,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout  # geocoding
        self.directions_read_timeout = directions_read_timeout
        self.max_retries = max_retries
        self.read_timeout_retries = read_timeout_retries
        self.backoff_seconds = backoff_seconds
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json, application/geo+json",
                "User-Agent": "fuel-route-optimizer",
            }
        )

    @property
    def configured(self) -> bool:
        """Whether an API key is available."""
        return bool(self._api_key)

    # Public API -------------------------------------------------------------------------

    def directions(
        self, points: Sequence[tuple[float, float]], profile: str = "driving-car"
    ) -> Route:
        """Route through ``points`` given as ``(lat, lon)``; one HTTP call (plus retries).

        ORS expects ``[lon, lat]`` order — the conversion happens here and only here.
        """
        if profile not in PROFILES:
            raise ValueError(f"Unsupported profile {profile!r}")
        body = {
            "coordinates": [[lon, lat] for lat, lon in points],
            "instructions": False,
            "radiuses": [SNAP_RADIUS_METERS] * len(points),
        }
        data = self._request(
            "POST",
            DIRECTIONS_PATH.format(profile=profile),
            read_timeout=self.directions_read_timeout,
            json=body,
        )
        try:
            feature = data["features"][0]
            summary = feature["properties"]["summary"]
            coordinates = [(float(c[0]), float(c[1])) for c in feature["geometry"]["coordinates"]]
            bbox = tuple(data["bbox"]) if len(data.get("bbox", [])) == 4 else None
            return Route(
                coordinates=coordinates,
                distance_m=float(summary.get("distance", 0.0)),
                duration_s=float(summary.get("duration", 0.0)),
                bbox=bbox,  # type: ignore[arg-type]
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RoutingProviderError("Unexpected directions response format.") from exc

    def geocode(self, text: str, size: int = 1) -> list[GeocodeResult]:
        """Free-text geocode restricted to the United States."""
        params = {"text": text, "boundary.country": "US", "size": size}
        return self._features(self._request("GET", GEOCODE_SEARCH_PATH, params=params))

    def geocode_structured(self, locality: str, region: str, size: int = 1) -> list[GeocodeResult]:
        """Structured geocode of a US city (``locality``) in a state (``region``)."""
        params = {"locality": locality, "region": region, "country": "USA", "size": size}
        return self._features(self._request("GET", GEOCODE_STRUCTURED_PATH, params=params))

    # Internals --------------------------------------------------------------------------

    @staticmethod
    def _features(data: dict[str, Any]) -> list[GeocodeResult]:
        try:
            return [GeocodeResult.from_feature(f) for f in data.get("features", [])]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RoutingProviderError("Unexpected geocoding response format.") from exc

    def _request(
        self, method: str, path: str, read_timeout: float | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if not self._api_key:
            raise RoutingNotConfiguredError()
        url = f"{self.base_url}{path}"
        headers = {"Authorization": self._api_key}
        # Two separate retry budgets. Both requests we make (directions, geocode) are
        # idempotent, so repeating them is safe.
        _record_call()
        transient_left = self.max_retries  # connection errors, 502/503/504
        timeouts_left = self.read_timeout_retries  # read timeouts: bounded worst-case wait
        attempt = 0

        while True:
            attempt += 1
            _record_attempt()
            started = time.perf_counter()
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=(self.connect_timeout, read_timeout or self.read_timeout),
                    **kwargs,
                )
            except requests.ReadTimeout as exc:
                logger.warning(
                    "ORS %s %s read timeout after %.0f ms (attempt %d)",
                    method, path, _ms(started), attempt,
                )  # fmt: skip
                if timeouts_left > 0:
                    timeouts_left -= 1
                    continue
                raise RoutingProviderTimeoutError() from exc
            except (requests.ConnectionError, requests.Timeout) as exc:  # incl. connect timeout
                logger.warning(
                    "ORS %s %s connection error (attempt %d): %s", method, path, attempt, exc
                )
                if transient_left > 0:
                    transient_left -= 1
                    self._sleep(attempt)
                    continue
                raise RoutingProviderUnavailableError() from exc

            logger.info(
                "ORS %s %s -> %d in %.0f ms (attempt %d)",
                method, path, response.status_code, _ms(started), attempt,
            )  # fmt: skip
            if response.status_code in _RETRYABLE_STATUS and transient_left > 0:
                transient_left -= 1
                self._sleep(attempt)
                continue
            return self._handle(response)

    def _handle(self, response: requests.Response) -> dict[str, Any]:
        status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = {}
        if 200 <= status < 300:
            if not isinstance(data, dict) or not data:
                raise RoutingProviderError("Routing provider returned an invalid body.")
            return data

        ors_code, ors_message = _error_details(data)
        details = {"provider_status": status, "provider_code": ors_code}
        if status in (401, 403):
            raise RoutingProviderUnavailableError(
                "The routing provider rejected the API key.", **details
            )
        if status == 429:
            raise RateLimitedError("Routing provider quota exceeded; retry later.", **details)
        if ors_code in ROUTE_NOT_POSSIBLE_CODES:
            raise RouteUnreachableError(ors_message or "No drivable route found.", **details)
        if status in (503, 504):
            raise RoutingProviderUnavailableError(**details)
        raise RoutingProviderError(**details, provider_message=ors_message)

    def _sleep(self, attempt: int) -> None:
        time.sleep(self.backoff_seconds * attempt)


def _error_details(data: Any) -> tuple[int | None, str]:
    """Extract ``(code, message)`` from ORS (``{"error": {...}}``) or gateway error bodies."""
    if not isinstance(data, dict):
        return None, ""
    error = data.get("error")
    if isinstance(error, str):  # gateway style: {"error": "Authorization field missing"}
        return None, error
    if not isinstance(error, dict):
        return None, ""
    try:
        code = int(error.get("code"))
    except (TypeError, ValueError):
        code = None
    return code, str(error.get("message", ""))


def _ms(started: float) -> float:
    return 1000 * (time.perf_counter() - started)


@lru_cache(maxsize=1)
def get_ors_client() -> ORSClient:
    """Process-wide client built from settings (``get_ors_client.cache_clear()`` rebuilds it)."""
    return ORSClient(
        api_key=settings.ORS_API_KEY,
        base_url=settings.ORS_BASE_URL,
        connect_timeout=settings.ORS_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.ORS_GEOCODE_READ_TIMEOUT_SECONDS,
        directions_read_timeout=settings.ORS_DIRECTIONS_READ_TIMEOUT_SECONDS,
        max_retries=settings.ORS_MAX_RETRIES,
        read_timeout_retries=settings.ORS_READ_TIMEOUT_RETRIES,
    )
