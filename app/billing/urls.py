from django.urls import include, path
from rest_framework.routers import SimpleRouter

from billing.views import InvoiceLineViewSet, InvoiceViewSet, PaymentViewSet

app_name = "billing"

router = SimpleRouter()
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("invoice-lines", InvoiceLineViewSet, basename="invoiceline")
router.register("payments", PaymentViewSet, basename="payment")

urlpatterns = [path("", include(router.urls))]
