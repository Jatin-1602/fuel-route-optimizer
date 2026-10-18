"""Shared pytest fixtures."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.routing.services.corridor import set_station_index
from apps.routing.services.ors_client import get_ors_client

FIXTURES = Path(__file__).parent / "fixtures" / "ors"
ORS_TEST_BASE_URL = "https://ors.test"


@pytest.fixture(autouse=True)
def _isolated_settings(settings) -> Iterator[None]:
    """In-memory cache, no real ORS key, and a fresh ORS client in every test."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    settings.ORS_API_KEY = ""
    settings.ORS_BASE_URL = ORS_TEST_BASE_URL
    cache.clear()
    get_ors_client.cache_clear()
    set_station_index(None)  # each test loads (or installs) its own station index
    yield
    get_ors_client.cache_clear()
    set_station_index(None)


@pytest.fixture
def ors_key(settings) -> str:
    """Configure a fake ORS key (requests are intercepted by `responses`, never sent)."""
    settings.ORS_API_KEY = "test-key-not-real"
    get_ors_client.cache_clear()
    return settings.ORS_API_KEY


def load_fixture(name: str) -> dict:
    """Load a captured ORS response from tests/fixtures/ors/."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def api_client() -> APIClient:
    """DRF test client (the Django counterpart of FastAPI's TestClient)."""
    return APIClient()
