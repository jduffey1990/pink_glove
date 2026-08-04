from django.urls import include, path
from rest_framework.routers import SimpleRouter

from customers.views import CustomerViewSet, ServiceLocationViewSet

app_name = "customers"

router = SimpleRouter()
router.register("customers", CustomerViewSet, basename="customer")
router.register("locations", ServiceLocationViewSet, basename="location")

urlpatterns = [path("", include(router.urls))]
