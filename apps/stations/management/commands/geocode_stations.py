"""``manage.py geocode_stations`` — clean the raw CSV and attach coordinates."""

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.routing.exceptions import RateLimitedError
from apps.routing.services.ors_client import get_ors_client
from apps.stations.services.cleaning import clean_fuel_csv
from apps.stations.services.gazetteer import (
    PlaceIndex,
    compute_state_bounds,
    read_county_keys,
    read_places_csv,
)
from apps.stations.services.ors_fallback import (
    FallbackCache,
    FallbackDecision,
    decide_all,
    fetch_missing,
)
from apps.stations.services.station_geocoder import (
    find_unmatched_pairs,
    geocode_stations,
    write_geocoded_csv,
    write_unmatched_csv,
)


class Command(BaseCommand):
    help = (
        "Clean data/fuel-prices-for-be-assessment.csv (US only, dedupe by OPIS ID keeping "
        "the lowest price), geocode each station to its city via data/us_places.csv, apply "
        "cached ORS fallback answers for cities the gazetteer lacks, and write "
        "data/stations_geocoded.csv plus a report of unmatched cities. Makes no network "
        "calls unless --ors-fallback is given."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--input", type=Path, default=settings.FUEL_PRICES_CSV)
        parser.add_argument("--places", type=Path, default=settings.US_PLACES_CSV)
        parser.add_argument("--output", type=Path, default=settings.STATIONS_GEOCODED_CSV)
        parser.add_argument("--unmatched", type=Path, default=settings.UNMATCHED_CITIES_CSV)
        parser.add_argument("--counties", type=Path, default=settings.US_COUNTIES_CSV)
        parser.add_argument("--ors-cache", type=Path, default=settings.ORS_FALLBACK_CACHE)
        parser.add_argument(
            "--ors-fallback",
            action="store_true",
            help="Query ORS (rate-limited, once per pair) for unmatched cities not yet cached.",
        )
        parser.add_argument(
            "--no-ors-cache",
            action="store_true",
            help="Ignore the ORS fallback cache entirely (pure offline gazetteer result).",
        )
        parser.add_argument(
            "--show-unmatched", type=int, default=20, help="How many unmatched cities to print"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["places"].exists():
            raise CommandError(
                f"{options['places']} not found. Run `python manage.py build_places` first."
            )
        if options["ors_fallback"] and options["no_ors_cache"]:
            raise CommandError("--ors-fallback and --no-ors-cache are mutually exclusive.")

        try:
            stations, cleaning = clean_fuel_csv(options["input"])
        except (OSError, ValueError) as exc:  # missing file, missing columns
            raise CommandError(f"Cannot read the fuel price CSV: {exc}") from exc
        places = read_places_csv(options["places"])
        index = PlaceIndex(places)

        decisions: dict[tuple[str, str], FallbackDecision] = {}
        calls = 0
        if not options["no_ors_cache"]:
            try:
                cache = FallbackCache(options["ors_cache"])
            except ValueError as exc:  # unsupported cache version / corrupt file
                raise CommandError(str(exc)) from exc
            pairs = find_unmatched_pairs(stations, index)
            if options["ors_fallback"]:
                calls = self._fetch(pairs, cache)
            bounds = compute_state_bounds(places)
            counties = read_county_keys(options["counties"])
            decisions = {(d.city, d.state): d for d in decide_all(pairs, cache, bounds, counties)}

        geocoded, report = geocode_stations(stations, index, decisions)
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
        if not options["no_ors_cache"]:
            accepted = [d for d in decisions.values() if d.accepted]
            rejected = [d for d in decisions.values() if not d.accepted]
            out(f"ORS fallback (cache: {options['ors_cache'].name})")
            out(f"  network calls this run       {calls:>6}")
            out(f"  cached answers used          {len(decisions):>6}")
            out(f"  accepted cities              {len(accepted):>6}")
            out(f"  rejected cities              {len(rejected):>6}")
        out(f"Geocoding (offline gazetteer: {len(index)} places)")
        out(f"  matched                      {report.matched:>6}  ({report.match_rate:.2%})")
        for method, count in sorted(report.matched_by.items()):
            out(f"    {method:<26} {count:>6}")
        out(
            f"  unmatched stations           {report.unmatched_stations:>6}  "
            f"({len(report.unmatched_cities)} distinct cities -> {options['unmatched']})"
        )
        for city in report.unmatched_cities[: options["show_unmatched"]]:
            hint = (
                f"ORS: {city.ors_result or '-'} [{city.ors_reason}]"
                if city.ors_reason
                else f"closest: {city.closest_gazetteer_name!r} {city.similarity:.2f}"
            )
            out(f"    {city.station_count:>3} x {city.city}, {city.state}  ({hint})")
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(geocoded)} stations -> {options['output']}")
        )

    def _fetch(self, pairs: list[tuple[str, str]], cache: FallbackCache) -> int:
        client = get_ors_client()
        if not client.configured:
            raise CommandError("--ors-fallback needs ORS_API_KEY in .env.")
        todo = sum(1 for city, state in pairs if f"{city}|{state}" not in cache)
        interval = settings.ORS_BATCH_GEOCODE_INTERVAL_SECONDS
        self.stdout.write(
            f"Querying ORS for {todo} uncached city/state pairs "
            f"(>= {interval:.1f}s apart, ~{todo * interval:.0f}s)..."
        )
        try:
            return fetch_missing(pairs, cache, client, interval)
        except RateLimitedError as exc:
            raise CommandError(
                f"ORS quota exhausted; progress saved, re-run later. ({exc})"
            ) from exc
