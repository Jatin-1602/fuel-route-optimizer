"""One place where every error becomes ``{"error": {"code", "message", "details"}}``."""

import logging
from typing import Any

from django.core.exceptions import RequestDataTooBig, SuspiciousOperation
from rest_framework import exceptions, status
from rest_framework.response import Response

from apps.routing.exceptions import DomainError

logger = logging.getLogger(__name__)


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    """The JSON error envelope used by every endpoint."""
    return {"error": {"code": code, "message": message, "details": details or {}}}


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    """DRF ``EXCEPTION_HANDLER``: domain errors, validation, throttling, and the rest."""
    if isinstance(exc, DomainError):
        if exc.status_code >= 500:
            logger.warning("%s: %s %s", exc.code, exc.message, exc.details)
        response = Response(error_body(exc.code, exc.message, exc.details), status=exc.status_code)
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            response["Retry-After"] = "60"
        return response

    if isinstance(exc, exceptions.ValidationError):
        details = exc.detail if isinstance(exc.detail, dict) else {"non_field_errors": exc.detail}
        return Response(
            error_body("invalid_input", "Invalid request parameters.", details),
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, exceptions.Throttled):
        wait = int(exc.wait or 1)
        response = Response(
            error_body(
                "rate_limited",
                f"Too many requests; retry in {wait} seconds.",
                {"retry_after_seconds": wait},
            ),
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
        response["Retry-After"] = str(wait)
        return response

    if isinstance(exc, RequestDataTooBig):  # body over DATA_UPLOAD_MAX_MEMORY_SIZE
        return Response(
            error_body("request_too_large", "The request body is too large."),
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    if isinstance(exc, SuspiciousOperation):  # e.g. too many fields; never echo details
        logger.warning("Suspicious request rejected: %s", type(exc).__name__)
        return Response(
            error_body("bad_request", "The request was rejected."),
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, exceptions.APIException):  # 404, 405, 415, parse errors, ...
        return Response(error_body(str(exc.default_code), str(exc.detail)), status=exc.status_code)

    logger.exception("Unhandled error", exc_info=exc)
    return Response(
        error_body("internal_error", "An unexpected error occurred."),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
