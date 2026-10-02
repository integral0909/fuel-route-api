class PlannerError(Exception):
    status_code = 400
    code = "planner_error"

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra  # structured context merged into the error response


class LocationNotFound(PlannerError):
    code = "location_not_found"


class LocationOutsideUSA(PlannerError):
    code = "location_outside_usa"


class NoRouteFound(PlannerError):
    status_code = 422
    code = "no_route"


class NoFeasibleFuelPlan(PlannerError):
    status_code = 422
    code = "no_feasible_fuel_plan"


class RoutingUnavailable(PlannerError):
    status_code = 502
    code = "routing_unavailable"
