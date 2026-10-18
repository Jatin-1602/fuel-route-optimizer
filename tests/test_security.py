"""Security regression tests (findings of the manual review; see ADR-020)."""

import json
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import responses
from django.conf import settings
from rest_framework.throttling import ScopedRateThrottle

from apps.routing.services.corridor import StationIndex, set_station_index
from apps.routing.services.ors_client import ORSClient
from apps.stations.services.station_geocoder import read_geocoded_csv
from tests.conftest import ORS_TEST_BASE_URL, load_fixture

REPO = Path(__file__).resolve().parent.parent
CAR_URL = f"{ORS_TEST_BASE_URL}/openrouteservice/v2/directions/driving-car/geojson"
SEARCH_URL = f"{ORS_TEST_BASE_URL}/pelias/v1/search"
ROUTE = "/api/v1/route/"
CHI_HOU = {"start": "Chicago, IL", "finish": "Houston, TX", "include_geometry": "false"}


@pytest.fixture(scope="module")
def rows() -> list[tuple]:
    return [
        (s.opis_id, s.name, s.address, s.city, s.state, float(s.price), s.lat, s.lon)
        for s in read_geocoded_csv(settings.STATIONS_GEOCODED_CSV)
    ]


@pytest.fixture(autouse=True)
def _stations(rows) -> None:
    set_station_index(StationIndex.from_rows(rows))


@pytest.fixture
def mocked(ors_key: str) -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.post(CAR_URL, json=load_fixture("directions_chicago_houston.json"))
        yield rsps


def django_subprocess(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    keep = {
        k: v for k, v in os.environ.items() if not k.startswith(("SECRET_KEY", "DEBUG", "ORS_"))
    }
    keep.update(DJANGO_SETTINGS_MODULE="config.settings", **env)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=keep, capture_output=True, text=True
    )


PRODUCTION = {
    "DEBUG": "False",
    "SECRET_KEY": secrets.token_urlsafe(50),
    "ALLOWED_HOSTS": "api.example.com",
}
HTTPS_ON = {
    "SECURE_SSL_REDIRECT": "True",
    "SECURE_HSTS_SECONDS": "31536000",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS": "True",
    "SECURE_HSTS_PRELOAD": "True",
    "SESSION_COOKIE_SECURE": "True",
    "CSRF_COOKIE_SECURE": "True",
}
CHECK_DEPLOY = (
    "import django; django.setup(); from django.core.management import call_command; "
    "call_command('check', deploy=True)"
)


# --- Throttling cannot be bypassed with X-Forwarded-For ---------------------------------


def test_forged_x_forwarded_for_does_not_bypass_the_throttle(api_client, mocked, monkeypatch):
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"route": "2/min"})
    statuses = [
        api_client.get(
            ROUTE, CHI_HOU, REMOTE_ADDR="10.9.9.9", HTTP_X_FORWARDED_FOR=f"1.2.3.{i}"
        ).status_code
        for i in range(4)
    ]
    assert statuses == [200, 200, 429, 429]


def test_behind_a_trusted_proxy_the_last_hop_identifies_clients(
    api_client, mocked, monkeypatch, settings
):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"route": "1/min"})
    # Two real clients behind one proxy (same REMOTE_ADDR) get separate budgets …
    a = api_client.get(ROUTE, CHI_HOU, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="5.5.5.5")
    b = api_client.get(ROUTE, CHI_HOU, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="6.6.6.6")
    # … and a client cannot escape by prepending forged hops (only the last one counts).
    forged = api_client.get(
        ROUTE, CHI_HOU, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="9.9.9.9, 5.5.5.5"
    )
    assert (a.status_code, b.status_code, forged.status_code) == (200, 200, 429)


# --- Production configuration -----------------------------------------------------------


def test_check_deploy_flags_only_https_settings_by_default() -> None:
    result = django_subprocess(CHECK_DEPLOY, **PRODUCTION)
    flagged = sorted(set(w for w in ("W004", "W008", "W012", "W016") if w in result.stderr))
    assert flagged == ["W004", "W008", "W012", "W016"], result.stderr
    assert "issues (0 silenced)" in result.stderr


def test_check_deploy_is_clean_with_https_settings_enabled() -> None:
    result = django_subprocess(CHECK_DEPLOY, **PRODUCTION, **HTTPS_ON)
    assert result.returncode == 0, result.stderr
    assert "no issues" in (result.stdout + result.stderr)


def test_plain_http_ors_base_url_is_refused_in_production() -> None:
    result = django_subprocess(
        "import django; django.setup()", **PRODUCTION, ORS_BASE_URL="http://api.heigit.org"
    )
    assert result.returncode != 0
    assert "ORS_BASE_URL must use https://" in result.stderr


def test_allowed_hosts_rejects_unknown_host(api_client) -> None:
    assert api_client.get("/api/health/", HTTP_HOST="evil.example").status_code == 400


# --- Errors never leak internals --------------------------------------------------------


@pytest.mark.parametrize("debug", [False, True])
def test_unhandled_error_is_generic_json_without_traceback(
    api_client, monkeypatch, settings, debug
):
    settings.DEBUG = debug

    def boom(*args, **kwargs):
        raise RuntimeError(f"internal detail {settings.SECRET_KEY}")

    monkeypatch.setattr("apps.routing.views.plan_trip", boom)
    response = api_client.get(ROUTE, CHI_HOU)

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred.",
            "details": {},
        }
    }
    body = response.content.decode()
    assert "Traceback" not in body and "internal detail" not in body
    assert settings.SECRET_KEY not in body


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "bad key {key}"),
        (500, {"code": 2099, "message": "Internal error for key {key}"}),  # echoed as 502
        (400, {"code": 2003, "message": "Parameter rejected: {key}"}),
    ],
)
def test_provider_error_messages_never_contain_the_key(api_client, ors_key, status, error) -> None:
    payload = (
        error.format(key=ors_key)
        if isinstance(error, str)
        else {**error, "message": error["message"].format(key=ors_key)}
    )
    with responses.RequestsMock() as rsps:
        rsps.post(CAR_URL, status=status, json={"error": payload})
        response = api_client.get(ROUTE, CHI_HOU)
    assert ors_key not in response.content.decode()
    assert response.status_code in (502, 503)


# --- Input validation and size limits ---------------------------------------------------


@pytest.mark.parametrize(
    "param", ["max_range_miles", "mpg", "corridor_miles", "origin_radius_miles", "stop_penalty"]
)
@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "1e309", "", "abc"])
def test_numeric_parameters_reject_non_finite_and_junk(api_client, param, value) -> None:
    response = api_client.get(ROUTE, {**CHI_HOU, param: value})
    assert response.status_code == 400
    assert param in response.json()["error"]["details"]


@pytest.mark.parametrize(("field", "limit"), [("start", 200), ("finish", 200), ("start_fuel", 20)])
def test_string_parameters_have_length_limits(api_client, field, limit) -> None:
    response = api_client.get(ROUTE, {**CHI_HOU, field: "x" * (limit + 1)})
    assert response.status_code == 400
    assert field in response.json()["error"]["details"]


@pytest.mark.parametrize("body", ["[]", '"text"', "123", "null"])
def test_non_object_json_body_is_invalid_input(api_client, body) -> None:
    response = api_client.post(ROUTE, body, content_type="application/json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


def test_request_body_size_is_capped_by_django(api_client) -> None:
    huge = json.dumps({"start": "Chicago, IL", "finish": "Houston, TX", "pad": "x" * 3_000_000})
    response = api_client.post(ROUTE, huge, content_type="application/json")
    assert response.status_code == 413  # DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB)
    assert response.json()["error"]["code"] == "request_too_large"


def test_too_many_query_fields_is_a_clean_400(api_client) -> None:
    many = "&".join(f"f{i}=1" for i in range(1100))  # DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
    response = api_client.get(f"{ROUTE}?start=Chicago,%20IL&finish=Houston,%20TX&{many}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


# --- Map page XSS --------------------------------------------------------------------------


def test_map_error_page_escapes_user_input(api_client) -> None:
    attack = '<script>alert("x")</script>, ON'
    html = api_client.get("/api/v1/route/map/", {"start": attack, "finish": "Chicago, IL"})
    text = html.content.decode()
    assert html.status_code == 422
    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text


def test_hostile_station_name_is_escaped_in_map_and_json(api_client, mocked, rows) -> None:
    hostile = "<img src=x onerror=alert(1)></script><script>alert(2)</script>"
    # QUIKTRIP #7205 (Lansing, IL) is stop 1 on the recorded Chicago -> Houston route.
    (first_stop_id,) = [r[0] for r in rows if r[1] == "QUIKTRIP #7205" and r[3] == "Lansing"]
    patched = [(r[0], hostile, *r[2:]) if r[0] == first_stop_id else r for r in rows]
    set_station_index(StationIndex.from_rows(patched))

    body = api_client.get(ROUTE, CHI_HOU).json()
    html = api_client.get("/api/v1/route/map/", CHI_HOU).content.decode()

    assert body["fuel_stops"][0]["name"] == hostile  # JSON carries the data verbatim (it's data)
    trip_json = html.split('id="trip-data" type="application/json">')[1].split("</script>")[0]
    assert "<" not in trip_json and "\\u003C" in trip_json  # json_script escaped it
    assert "onerror=alert(1)>" not in html.replace(trip_json, "")  # never rendered as markup
    assert ".innerHTML" not in html  # popups/table are built with textContent


# --- Outbound HTTP -------------------------------------------------------------------------


def test_user_text_never_changes_the_outbound_host(api_client, ors_key) -> None:
    with responses.RequestsMock() as rsps:  # only the configured ORS host is mocked
        rsps.get(SEARCH_URL, json={"type": "FeatureCollection", "features": []})
        response = api_client.get(
            ROUTE, {"start": "http://169.254.169.254/latest/meta-data", "finish": "Chicago, IL"}
        )
        assert [c.request.url.split("?")[0] for c in rsps.calls] == [SEARCH_URL]
    assert response.status_code == 422


def test_tls_verification_is_not_disabled() -> None:
    client = ORSClient("k", "https://api.heigit.org")
    assert client._session.verify is True
    sources = "".join(p.read_text() for p in (REPO / "apps").rglob("*.py"))
    assert "verify=False" not in sources
