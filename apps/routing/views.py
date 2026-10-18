"""HTTP views. Thin: validate -> plan (services) -> build payload."""

import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import django
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.routing.responses import build_map_context, build_route_response, data_version
from apps.routing.serializers import (
    ErrorResponseSerializer,
    HealthResponseSerializer,
    RouteRequestSerializer,
    RouteResponseSerializer,
)
from apps.routing.services.corridor import get_station_index
from apps.routing.services.geocoding import get_us_geography
from apps.routing.services.planner import plan_trip

logger = logging.getLogger(__name__)

ERROR_RESPONSES = {
    400: OpenApiResponse(ErrorResponseSerializer, description="invalid_input"),
    422: OpenApiResponse(
        ErrorResponseSerializer,
        description="location_not_found, location_outside_usa, same_location, route_unreachable",
    ),
    429: OpenApiResponse(ErrorResponseSerializer, description="rate_limited"),
    502: OpenApiResponse(ErrorResponseSerializer, description="routing_provider_error"),
    503: OpenApiResponse(
        ErrorResponseSerializer,
        description=(
            "routing_provider_unavailable (incl. missing API key), station_data_unavailable"
        ),
    ),
    504: OpenApiResponse(ErrorResponseSerializer, description="routing_provider_timeout"),
}
_QUERY_KEYS = (
    "start",
    "finish",
    "start_fuel",
    "max_range_miles",
    "mpg",
    "corridor_miles",
    "origin_radius_miles",
    "stop_penalty",
    "profile",
)


def _plain(data: Any) -> Any:
    """QueryDict -> dict (single values). Avoids DRF's HTML-form semantics, where a
    missing boolean field means ``False`` instead of its default."""
    return data.dict() if hasattr(data, "dict") else data


def _query_string(validated: dict[str, Any]) -> str:
    return urlencode({k: str(validated[k]) for k in _QUERY_KEYS})


def _response_cache_key(kind: str, validated: dict[str, Any]) -> str:
    """Key for a whole rendered payload: normalised inputs + every parameter + data version.

    A hit skips geocoding, routing, corridor search and optimisation entirely.
    """
    normalised = {
        k: " ".join(str(v).lower().split()) if k in ("start", "finish") else str(v)
        for k, v in sorted(validated.items())
    }
    raw = json.dumps({"kind": kind, "data": data_version(), **normalised}, sort_keys=True)
    return f"response:v1:{kind}:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _elapsed_ms(started: float) -> float:
    return round(1000 * (time.perf_counter() - started), 1)


def _validated(data: Any) -> RouteRequestSerializer:
    serializer = RouteRequestSerializer(data=_plain(data))
    serializer.is_valid(raise_exception=True)
    return serializer


class HealthView(APIView):
    """Liveness/readiness information. Never exposes secret values."""

    @extend_schema(responses=HealthResponseSerializer, tags=["health"])
    def get(self, request: Request) -> Response:
        """Service status, Django version, offline data in memory, and whether an ORS key is
        set. The first call loads the station index and gazetteer (~1 s), so calling it after
        startup leaves the process warm."""
        payload = {
            "status": "ok",
            "django_version": django.get_version(),
            "stations_loaded": len(get_station_index()),
            "places_loaded": len(get_us_geography().index),  # also warms the gazetteer
            "ors_api_key_configured": bool(settings.ORS_API_KEY),
        }
        return Response(HealthResponseSerializer(payload).data)


class RouteView(APIView):
    """Plan a route between two US locations with cost-optimal fuel stops."""

    throttle_scope = "route"

    @extend_schema(
        summary="Plan a route (query parameters)",
        parameters=[RouteRequestSerializer],
        responses={200: RouteResponseSerializer, **ERROR_RESPONSES},
        tags=["route"],
    )
    def get(self, request: Request) -> Response:
        return self._plan(request, request.query_params)

    @extend_schema(
        summary="Plan a route (JSON body)",
        request=RouteRequestSerializer,
        responses={200: RouteResponseSerializer, **ERROR_RESPONSES},
        tags=["route"],
        examples=[
            OpenApiExample(
                "Chicago to Houston, full tank",
                value={"start": "Chicago, IL", "finish": "Houston, TX", "start_fuel": "full"},
                request_only=True,
            )
        ],
    )
    def post(self, request: Request) -> Response:
        return self._plan(request, request.data)

    def _plan(self, request: Request, data: Any) -> Response:
        started = time.perf_counter()
        serializer = _validated(data)
        validated = serializer.validated_data
        map_url = (
            request.build_absolute_uri(reverse("v1:route-map")) + "?" + _query_string(validated)
        )
        key = _response_cache_key("json", validated)

        body = cache.get(key)
        if body is not None:
            logger.info("Response cache hit for %s -> %s", validated["start"], validated["finish"])
            body["meta"].update(
                external_api_calls=0,
                cache_hit=True,
                timings_ms={"response_cache": _elapsed_ms(started)},
            )
        else:
            trip = plan_trip(serializer.to_plan_params())
            building = time.perf_counter()
            body = build_route_response(
                trip,
                map_url="",
                include_geometry=validated["include_geometry"],
                timings_ms=dict(trip.timings_ms),
            )
            body["meta"]["timings_ms"]["response"] = _elapsed_ms(building)
            cache.set(key, body, settings.ROUTE_CACHE_TTL_SECONDS)

        body["start"]["query"], body["finish"]["query"] = validated["start"], validated["finish"]
        body["map_url"] = map_url
        body["meta"]["timings_ms"]["request_total"] = _elapsed_ms(started)
        return Response(body)


class MapRenderer(TemplateHTMLRenderer):
    """HTML renderer whose error responses use the map error page."""

    exception_template_names = ["routing/map_error.html"]


class RouteMapView(APIView):
    """The same trip as an interactive Leaflet map. Reuses the planner and its cache, so
    opening it after the JSON request makes no extra routing calls."""

    throttle_scope = "route"
    renderer_classes = [MapRenderer]

    @extend_schema(
        summary="Route map (HTML)",
        parameters=[RouteRequestSerializer],
        responses={(200, "text/html"): OpenApiTypes.STR},
        tags=["route"],
    )
    def get(self, request: Request) -> Response:
        serializer = _validated(request.query_params)
        validated = serializer.validated_data
        json_url = request.build_absolute_uri(reverse("v1:route")) + "?" + _query_string(validated)
        key = _response_cache_key("map", validated)

        context = cache.get(key)
        if context is not None:
            context["trip"].update(external_api_calls=0, cache_hit=True)
        else:
            context = build_map_context(plan_trip(serializer.to_plan_params()), json_url="")
            cache.set(key, context, settings.ROUTE_CACHE_TTL_SECONDS)
        context["json_url"] = json_url
        return Response(context, template_name="routing/map.html")
