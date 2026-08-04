from django.urls import include, path
from rest_framework.routers import SimpleRouter

from audit.views import AccessRevealViewSet

app_name = "audit"

router = SimpleRouter()
router.register("access-reveals", AccessRevealViewSet, basename="access-reveal")

urlpatterns = [path("", include(router.urls))]
