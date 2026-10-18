"""``manage.py geocode_stations`` — clean the raw CSV and attach offline coordinates."""

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.stations.services.cleaning import clean_fuel_csv
from apps.stations.services.gazetteer import PlaceIndex
from apps.stations.services.station_geocoder import (
    geocode_stations,
    write_geocoded_csv,
    write_unmatched_csv,
)


class Command(BaseCommand):
    help = (
        "Clean data/fuel-prices-for-be-assessment.csv (US only, dedupe by OPIS ID keeping "
        "the lowest price), geocode each station to its city via data/us_places.csv, and "
        "write data/stations_geocoded.csv plus a report of unmatched cities."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--input", type=Path, default=settings.FUEL_PRICES_CSV)
        parser.add_argument("--places", type=Path, default=settings.US_PLACES_CSV)
        parser.add_argument("--output", type=Path, default=settings.STATIONS_GEOCODED_CSV)
        parser.add_argument("--unmatched", type=Path, default=settings.UNMATCHED_CITIES_CSV)
        parser.add_argument(
            "--show-unmatched", type=int, default=20, help="How many unmatched cities to print"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["places"].exists():
            raise CommandError(
                f"{options['places']} not found. Run `python manage.py build_places` first."
            )
        stations, cleaning = clean_fuel_csv(options["input"])
        index = PlaceIndex.from_csv(options["places"])
        geocoded, report = geocode_stations(stations, index)
        write_geocoded_csv(geocoded, options["output"])
        write_unmatched_csv(report.unmatched_cities, options["unmatched"])

        out = self.stdout.write
        out("Cleaning")
        out(f"  rows in                      {cleaning.rows_in:>6}")
        non_us = sum(cleaning.non_us_dropped.values())
        out(f"  non-US rows dropped          {non_us:>6}  {dict(cleaning.non_us_dropped)}")
        out(f"  malformed rows dropped       {cleaning.malformed_dropped:>6}")
        out(f"  invalid price rows dropped   {cleaning.invalid_price_dropped:>6}")
        out(f"  duplicate rows merged        {cleaning.duplicate_rows_merged:>6}")
        out(f"  IDs with conflicting prices  {cleaning.ids_with_conflicting_prices:>6}")
        out(f"  unique US stations           {cleaning.stations_out:>6}")
        out(f"Geocoding (offline gazetteer: {len(index)} places)")
        out(f"  matched                      {report.matched:>6}  ({report.match_rate:.2%})")
        for method, count in sorted(report.matched_by.items()):
            out(f"    {method:<26} {count:>6}")
        out(
            f"  unmatched stations           {report.unmatched_stations:>6}  "
            f"({len(report.unmatched_cities)} distinct cities -> {options['unmatched']})"
        )
        for city in report.unmatched_cities[: options["show_unmatched"]]:
            out(
                f"    {city.station_count:>3} x {city.city}, {city.state}"
                f"  (closest: {city.closest_gazetteer_name!r} {city.similarity:.2f})"
            )
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(geocoded)} stations -> {options['output']}")
        )
