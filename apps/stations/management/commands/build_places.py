"""``manage.py build_places`` — derive data/us_places.csv from the Census Gazetteer."""

from argparse import ArgumentParser
from collections import Counter
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.stations.services.gazetteer import (
    build_places,
    download_gazetteer,
    read_gazetteer,
    write_places_csv,
)


class Command(BaseCommand):
    help = (
        "Download the US Census Gazetteer (places + county subdivisions) and write the "
        "compact offline gazetteer used for geocoding."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--year", type=int, default=settings.GAZETTEER_YEAR)
        parser.add_argument("--raw-dir", type=Path, default=settings.GAZETTEER_RAW_DIR)
        parser.add_argument("--output", type=Path, default=settings.US_PLACES_CSV)

    def handle(self, *args: Any, **options: Any) -> None:
        paths = download_gazetteer(options["year"], options["raw_dir"])
        records = [record for path in paths for record in read_gazetteer(path)]
        places = build_places(records)
        written = write_places_csv(places, options["output"])

        kinds = Counter(p.kind.name for p in places)
        self.stdout.write(f"Gazetteer {options['year']}: {len(records)} US records read")
        self.stdout.write(f"Places written: {written} -> {options['output']}")
        for kind, count in sorted(kinds.items()):
            self.stdout.write(f"  {kind:<20} {count}")
        self.stdout.write(self.style.SUCCESS("Done."))
