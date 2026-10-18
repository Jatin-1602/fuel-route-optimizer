"""Versioned API routes, included under /api/v1/ with namespace "v1"."""

from django.urls import path

from apps.routing.views import RouteMapView, RouteView

urlpatterns = [
    path("route/", RouteView.as_view(), name="route"),
    path("route/map/", RouteMapView.as_view(), name="route-map"),
]
