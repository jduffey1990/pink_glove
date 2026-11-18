from django.urls import include, path
from rest_framework.routers import SimpleRouter

from billing.views import (
    CollectionsCSVView,
    CollectionsReportView,
    InvoiceLineViewSet,
    InvoiceViewSet,
    PayCheckoutView,
    PayInvoiceCheckoutView,
    PayInvoiceView,
    PaymentViewSet,
    PublicInvoiceView,
    ReceivablesCSVView,
    ReceivablesReportView,
    StripeConnectView,
    StripeRefreshView,
    SubscriptionCheckoutView,
    SubscriptionPortalView,
    SubscriptionRefreshView,
)
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
    # The platform account's own events (the tenants' subscriptions, Phase
    # 4c): a second endpoint in Stripe with its own secret, the same view.
    path(
        "stripe/platform-webhook/",
        StripeWebhookView.as_view(platform=True),
        name="stripe-platform-webhook",
    ),
    path("stripe/connect/", StripeConnectView.as_view(), name="stripe-connect"),
    path("stripe/refresh/", StripeRefreshView.as_view(), name="stripe-refresh"),
    # The tenant's own subscription (Phase 4c). Reachable while read-only.
    path(
        "subscription/checkout/", SubscriptionCheckoutView.as_view(), name="subscription-checkout"
    ),
    path("subscription/portal/", SubscriptionPortalView.as_view(), name="subscription-portal"),
    path("subscription/refresh/", SubscriptionRefreshView.as_view(), name="subscription-refresh"),
    # Public, by signed token (the credential is in the invoice email). The
    # token travels in the body (ADR-036). These two are declared before the
    # deprecated path-token pair, whose `<str:token>` would otherwise take
    # `invoice` and `checkout` for a token.
    path("pay/invoice/", PublicInvoiceView.as_view(), name="pay-invoice"),
    path("pay/checkout/", PayCheckoutView.as_view(), name="pay-start-checkout"),
    path("pay/<str:token>/", PayInvoiceView.as_view(), name="pay"),
    path("pay/<str:token>/checkout/", PayInvoiceCheckoutView.as_view(), name="pay-checkout"),
    path("reports/receivables/", ReceivablesReportView.as_view(), name="report-receivables"),
    path("reports/receivables/csv/", ReceivablesCSVView.as_view(), name="report-receivables-csv"),
    path("reports/collections/", CollectionsReportView.as_view(), name="report-collections"),
    path("reports/collections/csv/", CollectionsCSVView.as_view(), name="report-collections-csv"),
    path("", include(router.urls)),
]
