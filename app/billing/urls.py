from django.urls import include, path
from rest_framework.routers import SimpleRouter

from billing.views import InvoiceLineViewSet, InvoiceViewSet, PaymentViewSet
from billing.webhook import StripeWebhookView

app_name = "billing"

router = SimpleRouter()
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("invoice-lines", InvoiceLineViewSet, basename="invoiceline")
router.register("payments", PaymentViewSet, basename="payment")

urlpatterns = [
    # Stripe calls this, nothing in ui/ does: a plain Django view outside the
    # schema, verified by signature rather than session (billing/webhook.py).
    path("stripe/webhook/", StripeWebhookView.as_view(), name="stripe-webhook"),
    path("", include(router.urls)),
]
