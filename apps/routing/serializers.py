"""DRF serializers for the routing API."""

from rest_framework import serializers


class HealthResponseSerializer(serializers.Serializer):
    """Shape of the ``GET /api/health/`` response (used for the OpenAPI schema)."""

    status = serializers.CharField()
    django_version = serializers.CharField()
    stations_loaded = serializers.IntegerField()
    ors_api_key_configured = serializers.BooleanField()
