from django.conf import settings
from rest_framework import serializers


class RoutePlanRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=300, help_text='"City, ST", an address, or "lat,lon"')
    finish = serializers.CharField(max_length=300)
    start_fuel = serializers.FloatField(
        required=False,
        default=0.0,
        min_value=0.0,
        max_value=1.0,
        help_text="Fraction of a full tank at departure",
    )
    corridor_miles = serializers.FloatField(
        required=False,
        default=None,
        allow_null=True,
        min_value=0.5,
        max_value=settings.FUEL_PLANNER["MAX_CORRIDOR_MILES"],
    )
    stop_penalty = serializers.FloatField(
        required=False,
        default=None,
        allow_null=True,
        min_value=0.0,
        max_value=100.0,
        help_text="$ per stop; 0 gives the strictly cheapest plan",
    )


class ErrorSerializer(serializers.Serializer):
    error = serializers.CharField(help_text="Machine-readable code, e.g. location_not_found")
    detail = serializers.JSONField()
