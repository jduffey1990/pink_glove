from django.urls import include, path
from rest_framework.routers import SimpleRouter

from catalog.views import ServiceViewSet

app_name = "catalog"

router = SimpleRouter()
router.register("services", ServiceViewSet, basename="service")

urlpatterns = [path("", include(router.urls))]
