"""HTTP views for the routing API."""

import django
from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.routing.serializers import HealthResponseSerializer
from apps.routing.services.corridor import get_station_index


class HealthView(APIView):
    """Liveness/readiness information. Never exposes secret values."""

    @extend_schema(responses=HealthResponseSerializer, tags=["health"])
    def get(self, request: Request) -> Response:
        """Return service status, Django version, station count, and whether an ORS key is set."""
        payload = {
            "status": "ok",
            "django_version": django.get_version(),
            "stations_loaded": len(get_station_index()),  # in-memory index the API serves from
            "ors_api_key_configured": bool(settings.ORS_API_KEY),
        }
        return Response(HealthResponseSerializer(payload).data)
