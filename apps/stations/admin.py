"""Django admin registration for fuel stations."""

from django.contrib import admin

from apps.stations.models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    """Browse/search stations; ordered by price so the cheapest are on top."""

    list_display = ("opis_id", "name", "city", "state", "price", "geocode_source", "geocode_match")
    list_filter = ("state", "geocode_source", "geocode_match")
    search_fields = ("name", "city", "address", "=opis_id")
    ordering = ("price",)
    readonly_fields = ("updated_at",)
    list_per_page = 50
