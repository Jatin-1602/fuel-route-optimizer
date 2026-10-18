"""Domain exceptions. Each carries its API error code and HTTP status.

Services raise these; a single DRF exception handler turns them into
``{"error": {"code", "message", "details"}}`` responses.
"""

from typing import Any


class DomainError(Exception):
    """Base class for errors that map to a client-facing API error."""

    code: str = "error"
    status_code: int = 500
    default_message: str = "Unexpected error."

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


# --- 4xx: the request cannot be served -------------------------------------------------


class InvalidInputError(DomainError):
    code, status_code, default_message = "invalid_input", 400, "Invalid input."


class LocationNotFoundError(DomainError):
    code, status_code, default_message = "location_not_found", 422, "Location not found."


class LocationOutsideUSAError(DomainError):
    code, status_code = "location_outside_usa", 422
    default_message = "Location must be within the United States."


class SameLocationError(DomainError):
    code, status_code = "same_location", 422
    default_message = "Start and finish resolve to the same location."


class RouteUnreachableError(DomainError):
    code, status_code = "route_unreachable", 422
    default_message = "No feasible route or fuel plan exists for this trip."


class RateLimitedError(DomainError):
    code, status_code, default_message = "rate_limited", 429, "Too many requests."


# --- 5xx: the routing provider failed --------------------------------------------------


class RoutingProviderError(DomainError):
    code, status_code = "routing_provider_error", 502
    default_message = "The routing provider returned an error."


class RoutingProviderUnavailableError(DomainError):
    code, status_code = "routing_provider_unavailable", 503
    default_message = "The routing provider is unavailable."


class RoutingNotConfiguredError(RoutingProviderUnavailableError):
    default_message = "Routing is not configured (ORS_API_KEY is missing)."


class RoutingProviderTimeoutError(DomainError):
    code, status_code = "routing_provider_timeout", 504
    default_message = "The routing provider timed out."


class StationDataUnavailableError(DomainError):
    code, status_code = "station_data_unavailable", 503
    default_message = "No fuel stations are loaded. Run `python manage.py load_stations`."
