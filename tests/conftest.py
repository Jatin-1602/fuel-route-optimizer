"""Shared pytest fixtures."""

import pytest
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def _isolated_settings(settings) -> None:
    """Use an in-memory cache and no real ORS key in every test."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    settings.ORS_API_KEY = ""


@pytest.fixture
def api_client() -> APIClient:
    """DRF test client (the Django counterpart of FastAPI's TestClient)."""
    return APIClient()
