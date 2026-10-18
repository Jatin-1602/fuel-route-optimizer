"""Load geocoded stations into the database (idempotent upsert)."""

from dataclasses import dataclass
from pathlib import Path

from django.db import transaction

from apps.stations.models import FuelStation
from apps.stations.services.station_geocoder import GeocodedStation, read_geocoded_csv

_UPDATE_FIELDS: tuple[str, ...] = (
    "name",
    "address",
    "city",
    "state",
    "rack_id",
    "price",
    "lat",
    "lon",
    "geocode_source",
    "geocode_match",
    "source_rows",
    "updated_at",
)


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Row counts from one :func:`load_stations` run."""

    total: int
    created: int
    updated: int
    deleted: int


def load_stations(stations: list[GeocodedStation], batch_size: int = 1000) -> LoadResult:
    """Upsert ``stations`` by ``opis_id`` and delete rows not in ``stations``.

    Runs in a single transaction, so readers never see a half-loaded table and running
    it twice yields the same table.
    """
    incoming_ids = {s.opis_id for s in stations}
    objects = [
        FuelStation(
            opis_id=s.opis_id,
            name=s.name,
            address=s.address,
            city=s.city,
            state=s.state,
            rack_id=s.rack_id,
            price=s.price,
            lat=s.lat,
            lon=s.lon,
            geocode_source=s.geocode_source,
            geocode_match=s.geocode_match,
            source_rows=s.source_rows,
        )
        for s in stations
    ]
    with transaction.atomic():
        existing_ids = set(FuelStation.objects.values_list("opis_id", flat=True))
        deleted, _ = FuelStation.objects.exclude(opis_id__in=incoming_ids).delete()
        FuelStation.objects.bulk_create(
            objects,
            batch_size=batch_size,
            update_conflicts=True,
            unique_fields=["opis_id"],
            update_fields=list(_UPDATE_FIELDS),
        )
    created = len(incoming_ids - existing_ids)
    return LoadResult(
        total=len(objects), created=created, updated=len(objects) - created, deleted=deleted
    )


def load_stations_from_csv(path: Path) -> LoadResult:
    """Read ``data/stations_geocoded.csv`` (or another path) and load it."""
    return load_stations(read_geocoded_csv(path))
