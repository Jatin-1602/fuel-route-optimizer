"""Database loading of geocoded stations (idempotent upsert) and the management commands."""

from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from apps.stations.models import FuelStation
from apps.stations.services.loader import load_stations
from apps.stations.services.station_geocoder import GeocodedStation, write_geocoded_csv

pytestmark = pytest.mark.django_db


def _station(opis_id: int, price: str = "3.50000000") -> GeocodedStation:
    return GeocodedStation(
        opis_id=opis_id,
        name=f"STOP {opis_id}",
        address="I-35, EXIT 1",
        city="Dallas",
        state="TX",
        rack_id=100,
        price=Decimal(price),
        lat=32.79,
        lon=-96.77,
        geocode_source="census_place",
        geocode_match="exact",
    )


def test_load_is_idempotent() -> None:
    stations = [_station(1), _station(2)]

    first = load_stations(stations)
    second = load_stations(stations)

    assert (first.created, first.updated, first.deleted) == (2, 0, 0)
    assert (second.created, second.updated, second.deleted) == (0, 2, 0)
    assert FuelStation.objects.count() == 2


def test_reload_updates_prices_and_removes_stale_stations() -> None:
    load_stations([_station(1), _station(2)])

    result = load_stations([replace(_station(1), price=Decimal("2.99900000")), _station(3)])

    assert (result.created, result.updated, result.deleted) == (1, 1, 1)
    assert set(FuelStation.objects.values_list("opis_id", flat=True)) == {1, 3}
    assert FuelStation.objects.get(opis_id=1).price == Decimal("2.999")


def test_price_keeps_all_csv_decimals() -> None:
    load_stations([_station(1, "3.00733333")])
    assert FuelStation.objects.get(opis_id=1).price == Decimal("3.00733333")


def test_load_stations_command(tmp_path: Path) -> None:
    path = tmp_path / "stations.csv"
    write_geocoded_csv([_station(1), _station(2)], path)
    out = StringIO()

    call_command("load_stations", "--input", str(path), stdout=out)

    assert "Loaded 2 stations" in out.getvalue()
    assert FuelStation.objects.count() == 2


def test_load_stations_command_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="geocode_stations"):
        call_command("load_stations", "--input", str(tmp_path / "missing.csv"))


def test_geocode_stations_command_on_real_data(tmp_path: Path) -> None:
    out = StringIO()
    call_command(
        "geocode_stations",
        "--output", str(tmp_path / "geo.csv"),
        "--unmatched", str(tmp_path / "unmatched.csv"),
        stdout=out,
    )  # fmt: skip
    text = out.getvalue()
    assert "non-US rows dropped             620" in text
    assert (tmp_path / "geo.csv").exists()
    assert (tmp_path / "unmatched.csv").exists()


def test_health_reports_station_count(api_client) -> None:
    load_stations([_station(1), _station(2), _station(3)])
    assert api_client.get(reverse("health")).json()["stations_loaded"] == 3
