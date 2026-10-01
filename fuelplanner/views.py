from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RoutePlanRequestSerializer
from .services.errors import PlannerError
from .services.planner import plan_trip

OPTIONAL_PARAMS = ("corridor_miles", "stop_penalty")


def run_plan(data):
    return plan_trip(
        data["start"],
        data["finish"],
        start_fuel_fraction=data["start_fuel"],
        corridor_miles=data["corridor_miles"],
        stop_penalty=data["stop_penalty"],
    )


class RoutePlanView(APIView):
    def get(self, request):
        return self.plan(request, request.query_params)

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
        if not ser.is_valid():
            ctx["error"], status = _format_errors(ser.errors), 400
        else:
            try:
                ctx["plan"] = run_plan(ser.validated_data)
            except PlannerError as e:
                ctx["error"], status = str(e), e.status_code
    return render(request, "fuelplanner/map.html", ctx, status=status)


def _format_errors(errors):
    return "; ".join(f"{field}: {' '.join(map(str, msgs))}" for field, msgs in errors.items())
