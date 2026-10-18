"""``manage.py load_stations`` — idempotently load geocoded stations into the database."""

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.stations.services.loader import load_stations_from_csv


class Command(BaseCommand):
    help = (
        "Upsert data/stations_geocoded.csv into the FuelStation table (by OPIS ID) and "
        "delete stations no longer present. Safe to run repeatedly."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--input", type=Path, default=settings.STATIONS_GEOCODED_CSV)

    def handle(self, *args: Any, **options: Any) -> None:
        path: Path = options["input"]
        if not path.exists():
            raise CommandError(f"{path} not found. Run `python manage.py geocode_stations` first.")
        result = load_stations_from_csv(path)
        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {result.total} stations from {path} "
                f"(created {result.created}, updated {result.updated}, deleted {result.deleted})."
            )
        )
