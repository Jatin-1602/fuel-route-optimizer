"""Fuel station models."""

from django.db import models


class FuelStation(models.Model):
    """A US truck stop with its posted retail price and city-level coordinates.

    Populated from ``data/stations_geocoded.csv`` by ``manage.py load_stations``.
    """

    class GeocodeSource(models.TextChoices):
        CENSUS_PLACE = "census_place", "Census place"
        CENSUS_COUSUB = "census_cousub", "Census county subdivision"
        ORS = "ors", "OpenRouteService geocoder"

    class GeocodeMatch(models.TextChoices):
        EXACT = "exact", "Exact name"
        COMPACT = "compact", "Exact ignoring spaces"
        FUZZY = "fuzzy", "Fuzzy name"

    opis_id = models.PositiveIntegerField(unique=True, help_text="OPIS Truckstop ID")
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    price = models.DecimalField(
        max_digits=12, decimal_places=8, help_text="Retail price, USD per gallon (as in the CSV)"
    )
    lat = models.FloatField()
    lon = models.FloatField()
    geocode_source = models.CharField(max_length=20, choices=GeocodeSource.choices)
    geocode_match = models.CharField(max_length=10, choices=GeocodeMatch.choices)
    source_rows = models.PositiveSmallIntegerField(
        default=1, help_text="Raw CSV rows merged into this station (duplicates)"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["opis_id"]
        indexes = [models.Index(fields=["price"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
