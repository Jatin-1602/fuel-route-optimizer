"""DRF serializers: request validation for the route endpoints and response schemas."""

from typing import Any

from django.conf import settings
from rest_framework import serializers

from apps.routing.services.optimizer import StartFuel, Vehicle
from apps.routing.services.ors_client import PROFILES
from apps.routing.services.planner import PlanParams

# --- Requests -------------------------------------------------------------------------------


class RouteRequestSerializer(serializers.Serializer):
    """Parameters of ``GET``/``POST /api/v1/route/`` (and the map page)."""

    start = serializers.CharField(
        max_length=200,
        help_text='"City, ST" (e.g. "New York, NY"), "lat,lon", or a US street address.',
    )
    finish = serializers.CharField(max_length=200, help_text="Same formats as `start`.")
    start_fuel = serializers.CharField(
        default="empty",
        max_length=20,
        help_text='"empty" (default), "full", or gallons in the tank (e.g. "12.5").',
    )
    max_range_miles = serializers.FloatField(default=500, min_value=50, max_value=1500)
    mpg = serializers.FloatField(default=10, min_value=1, max_value=100)
    corridor_miles = serializers.FloatField(
        default=10, min_value=1, max_value=50, help_text="Max distance from the route to a station."
    )
    origin_radius_miles = serializers.FloatField(
        default=25,
        min_value=1,
        max_value=100,
        help_text="start_fuel=empty: the first stop must be within this distance.",
    )
    stop_penalty = serializers.FloatField(
        default=lambda: settings.DEFAULT_STOP_PENALTY,
        min_value=0,
        max_value=200,
        help_text="USD per stop added when choosing stops (0 = cheapest fuel, any stop count).",
    )
    profile = serializers.ChoiceField(choices=PROFILES, default="driving-car")
    include_geometry = serializers.BooleanField(
        default=True, help_text="Include the GeoJSON route (large); false for lean responses."
    )

    def validate_start_fuel(self, value: str) -> StartFuel:
        try:
            return StartFuel.parse(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        tank = Vehicle(attrs["max_range_miles"], attrs["mpg"]).tank_gallons
        start_fuel: StartFuel = attrs["start_fuel"]
        if start_fuel.gallons is not None and start_fuel.gallons > tank + 1e-9:
            raise serializers.ValidationError(
                {"start_fuel": [f"{start_fuel.gallons:g} gal exceeds the tank ({tank:g} gal)."]}
            )
        return attrs

    def to_plan_params(self) -> PlanParams:
        """Validated data as planner input."""
        data = self.validated_data
        return PlanParams(
            start=data["start"],
            finish=data["finish"],
            start_fuel=data["start_fuel"],
            max_range_miles=data["max_range_miles"],
            mpg=data["mpg"],
            corridor_miles=data["corridor_miles"],
            origin_radius_miles=data["origin_radius_miles"],
            profile=data["profile"],
            stop_penalty=data["stop_penalty"],
        )


# --- Responses (schema documentation; the payload is built in responses.py) ---------------


class HealthResponseSerializer(serializers.Serializer):
    """Shape of the ``GET /api/health/`` response."""

    status = serializers.CharField()
    django_version = serializers.CharField()
    stations_loaded = serializers.IntegerField()
    places_loaded = serializers.IntegerField(help_text="Offline gazetteer entries in memory.")
    ors_api_key_configured = serializers.BooleanField()


class LocationSerializer(serializers.Serializer):
    query = serializers.CharField()
    name = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    resolved_by = serializers.ChoiceField(choices=["coordinates", "offline", "ors"])


class VehicleSerializer(serializers.Serializer):
    max_range_miles = serializers.FloatField()
    mpg = serializers.FloatField()
    tank_gallons = serializers.FloatField()


class PureOptimumSerializer(serializers.Serializer):
    total_fuel_cost = serializers.FloatField()
    number_of_stops = serializers.IntegerField()


class SummarySerializer(serializers.Serializer):
    total_distance_miles = serializers.FloatField()
    estimated_duration_hours = serializers.FloatField()
    fuel_consumed_gallons = serializers.FloatField()
    gallons_purchased = serializers.FloatField(help_text="Bought at the fuel stops.")
    initial_fuel_gallons = serializers.FloatField(
        help_text="In the tank at the start (empty: burned to reach stop 1)."
    )
    initial_fuel_cost = serializers.FloatField(
        help_text="empty: initial fuel charged at stop 1's price; otherwise 0."
    )
    total_fuel_cost = serializers.FloatField(help_text="Money spent on fuel for this trip.")
    average_price_paid = serializers.FloatField()
    average_corridor_price = serializers.FloatField()
    baseline_cost_at_avg_corridor_price = serializers.FloatField()
    estimated_savings = serializers.FloatField()
    number_of_stops = serializers.IntegerField()
    largest_leg_miles = serializers.FloatField(help_text="Longest drive between fuel sources.")
    fuel_at_finish_gallons = serializers.FloatField()
    stop_penalty = serializers.FloatField()
    pure_cost_optimum = PureOptimumSerializer(help_text="The same trip with stop_penalty=0.")
    extra_cost_vs_pure_optimum = serializers.FloatField()
    vehicle = VehicleSerializer()
    start_fuel = serializers.CharField()


class FuelStopSerializer(serializers.Serializer):
    order = serializers.IntegerField()
    station_id = serializers.IntegerField(help_text="OPIS Truckstop ID.")
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    price_per_gallon = serializers.FloatField()
    mile_marker = serializers.FloatField()
    off_route_miles = serializers.FloatField()
    fuel_on_arrival_gallons = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    cost = serializers.FloatField()
    fuel_on_departure_gallons = serializers.FloatField()


class MetaSerializer(serializers.Serializer):
    external_api_calls = serializers.IntegerField(
        help_text="Routing/geocoding API requests made for this response (0 on a cache hit)."
    )
    external_api_attempts = serializers.IntegerField(
        help_text="HTTP attempts incl. retries (> calls only if the provider timed out or failed)."
    )
    cache_hit = serializers.BooleanField()
    timings_ms = serializers.DictField(child=serializers.FloatField())
    stations_considered = serializers.IntegerField()
    corridor_miles = serializers.FloatField()
    profile = serializers.CharField()
    data_version = serializers.CharField()
    api_version = serializers.CharField()


class RouteResponseSerializer(serializers.Serializer):
    start = LocationSerializer()
    finish = LocationSerializer()
    summary = SummarySerializer()
    fuel_stops = FuelStopSerializer(many=True)
    route = serializers.JSONField(
        allow_null=True, help_text="GeoJSON FeatureCollection (null if include_geometry=false)."
    )
    map_url = serializers.CharField(help_text="Absolute URL of the HTML map for this trip.")
    assumptions = serializers.ListField(child=serializers.CharField())
    meta = MetaSerializer()


class ErrorDetailSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()
    details = serializers.JSONField()


class ErrorResponseSerializer(serializers.Serializer):
    error = ErrorDetailSerializer()
