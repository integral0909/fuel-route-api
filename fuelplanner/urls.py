from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from . import views

urlpatterns = [
    path("api/route-plan/", views.RoutePlanView.as_view(), name="route-plan"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
    path("map/", views.route_map, name="route-map"),
    path("healthz", views.healthz, name="healthz"),
]
