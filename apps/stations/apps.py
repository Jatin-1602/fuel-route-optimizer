from django.apps import AppConfig


class StationsConfig(AppConfig):
    """Fuel station data: model, admin, and CSV/geocoding management commands."""

    name = "apps.stations"
    verbose_name = "Fuel stations"
