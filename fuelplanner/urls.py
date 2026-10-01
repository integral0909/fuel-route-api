from django.urls import path

from . import views

urlpatterns = [
    path("api/route-plan/", views.RoutePlanView.as_view(), name="route-plan"),
    path("map/", views.route_map, name="route-map"),
]
