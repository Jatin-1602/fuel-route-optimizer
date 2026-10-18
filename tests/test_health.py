"""Tests for the health endpoint and API docs wiring."""

import django
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_health_ok(api_client) -> None:
    response = api_client.get(reverse("health"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["django_version"] == django.get_version()
    assert body["stations_loaded"] == 0
    assert body["places_loaded"] > 40_000
    assert body["ors_api_key_configured"] is False


def test_health_reports_key_presence_without_leaking_it(api_client, settings) -> None:
    settings.ORS_API_KEY = "super-secret-test-key"

    response = api_client.get(reverse("health"))

    assert response.json()["ors_api_key_configured"] is True
    assert "super-secret-test-key" not in response.content.decode()


def test_openapi_schema_and_swagger_ui(api_client) -> None:
    schema = api_client.get(reverse("schema"))
    assert schema.status_code == 200
    assert b"/api/health/" in schema.content

    docs = api_client.get(reverse("swagger-ui"))
    assert docs.status_code == 200
