"""``manage.py clear_cache`` — invalidate cached routes, responses and geocodes."""

from argparse import ArgumentParser
from typing import Any

from django.core.cache import cache
from django.core.management.base import BaseCommand

from apps.routing.services.cache_keys import NAMESPACES, invalidate


class Command(BaseCommand):
    help = (
        "Invalidate the route, response and geocode caches (all by default) so the next "
        "requests are cold. Works across running server processes that share the cache. "
        "Throttle counters are kept unless --all is given."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--only",
            choices=NAMESPACES,
            action="append",
            help="Invalidate just this namespace (repeatable).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Wipe the whole cache backend, including throttle counters.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["all"]:
            cache.clear()
            self.stdout.write(
                self.style.SUCCESS("Cleared the entire cache (incl. throttle counters).")
            )
            return
        for namespace in options["only"] or NAMESPACES:
            generation = invalidate(namespace)
            self.stdout.write(f"  {namespace:<9} cache invalidated (generation {generation})")
        self.stdout.write(self.style.SUCCESS("Done."))
