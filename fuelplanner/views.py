from urllib.parse import urlencode

from django.db import DatabaseError
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FuelStation
from .serializers import ErrorSerializer, RoutePlanRequestSerializer
from .services.errors import PlannerError
from .services.planner import plan_trip
from .throttling import RoutePlanThrottle, map_view_throttled

OPTIONAL_PARAMS = ("corridor_miles", "stop_penalty")

ERROR_RESPONSES = {
    400: ErrorSerializer,
    422: ErrorSerializer,
    429: OpenApiTypes.OBJECT,
    502: ErrorSerializer,
}


def run_plan(data):
    return plan_trip(
        data["start"],
        data["finish"],
        start_fuel_fraction=data["start_fuel"],
        corridor_miles=data["corridor_miles"],
        stop_penalty=data["stop_penalty"],
    )


class RoutePlanView(APIView):
    throttle_classes = [RoutePlanThrottle]

    @extend_schema(
        summary="Plan fuel stops (query string)",
        parameters=[RoutePlanRequestSerializer],
        responses={200: OpenApiTypes.OBJECT, **ERROR_RESPONSES},
    )
    def get(self, request):
        return self.plan(request, request.query_params)

    @extend_schema(
        summary="Plan fuel stops (JSON body)",
        request=RoutePlanRequestSerializer,
        responses={200: OpenApiTypes.OBJECT, **ERROR_RESPONSES},
        examples=[
            OpenApiExample(
                "Half tank",
                value={"start": "Chicago, IL", "finish": "Houston, TX", "start_fuel": 0.5},
                request_only=True,
            )
        ],
    )
    def post(self, request):
        return self.plan(request, request.data)

    def plan(self, request, params):
        ser = RoutePlanRequestSerializer(data=params)
        if not ser.is_valid():
            return Response({"error": "invalid_request", "detail": ser.errors}, status=400)
        data = ser.validated_data

        try:
            result = run_plan(data)
        except PlannerError as e:
            return Response({"error": e.code, "detail": str(e)}, status=e.status_code)

        query = {"start": data["start"], "finish": data["finish"], "start_fuel": data["start_fuel"]}
        query.update({k: data[k] for k in OPTIONAL_PARAMS if data[k] is not None})
        result["map"]["html_url"] = request.build_absolute_uri(
            reverse("route-map") + "?" + urlencode(query)
        )
        return Response(result)


def route_map(request):
    ctx = {"query": request.GET, "plan": None, "error": None}
    status = 200
    if "start" in request.GET or "finish" in request.GET:
        ser = RoutePlanRequestSerializer(data=request.GET)
        if map_view_throttled(request):
            ctx["error"], status = "Too many requests, try again in a minute.", 429
        elif not ser.is_valid():
            ctx["error"], status = _format_errors(ser.errors), 400
        else:
            try:
                ctx["plan"] = run_plan(ser.validated_data)
            except PlannerError as e:
                ctx["error"], status = str(e), e.status_code
    return render(request, "fuelplanner/map.html", ctx, status=status)


def healthz(request):
    try:
        stations = FuelStation.objects.count()
    except DatabaseError:
        return JsonResponse({"status": "error", "detail": "database unavailable"}, status=503)
    if not stations:
        return JsonResponse(
            {"status": "error", "detail": "no stations loaded, run load_stations"}, status=503
        )
    return JsonResponse({"status": "ok", "stations": stations})


def _format_errors(errors):
    return "; ".join(f"{field}: {' '.join(map(str, msgs))}" for field, msgs in errors.items())
