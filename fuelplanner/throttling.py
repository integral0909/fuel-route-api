from rest_framework.request import Request
from rest_framework.throttling import AnonRateThrottle


class RoutePlanThrottle(AnonRateThrottle):
    scope = "route_plan"


def map_view_throttled(django_request):
    """The map page is a plain Django view but plans routes too, so it shares the API limit."""
    throttle = RoutePlanThrottle()
    return not throttle.allow_request(Request(django_request), view=None)
