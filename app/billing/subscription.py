"""
The platform subscription: the one module that talks to Stripe on the
platform's own behalf (Phase 4c, ADR-024, ADR-028).

Everything here runs on the platform account -- the tenant is Stripe's
*customer* here, where in `billing.connect` it is a connected account. The
whole difference between the two modules is that nothing below passes
`stripe_account=`; the client, the error translation and the "optional on
the server" rule are `connect`'s, reused. The coverage rule the fields feed
is `organizations.services`, which this module calls and never duplicates.

Platform billing is optional on top of Stripe. With no
`STRIPE_PLATFORM_PRICE_ID` the entry points raise `ServiceUnavailableError`
(a 503), nobody is deactivated for billing, and the product is exactly 4b.

Tests patch `connect.client()` -- nothing below reaches the network.
"""

import datetime as dt
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from app.exceptions import ConflictError, ServiceUnavailableError
from billing import connect
from organizations import services as coverage
from organizations.models import Organization

logger = logging.getLogger(__name__)

NOT_ENABLED = "Subscriptions are not configured on this server."
ALREADY_SUBSCRIBED = (
    "This organization already has a subscription. Manage it from the billing portal."
)
EXEMPT = "This organization is complimentary and needs no subscription."
DEACTIVATED_BY_HAND = (
    "This organization was deactivated by pink glove, not for billing, so a subscription "
    "would not restore it. Contact support."
)
NO_CUSTOMER = "There is nothing to manage yet. Subscribe first."


#: One rule, in `organizations.services`, so the serializer and this module agree.
enabled = coverage.billing_enabled


def _require_enabled() -> None:
    if not enabled():
        raise ServiceUnavailableError(NOT_ENABLED)


# ---------------------------------------------------------------------------
# Subscribing
# ---------------------------------------------------------------------------


@transaction.atomic
def start_checkout(organization: Organization, *, actor=None) -> str:
    """
    A Stripe-hosted page that starts the subscription. Returns its URL.

    The Customer is created on the first call and never again, under the
    organization's row lock so two owners pressing Subscribe at once share
    one; a Checkout Session is created every time, because they expire. The
    organization id rides on the session and on the Subscription it will
    create -- the Subscription is what later events name, and it does not
    inherit the session's metadata. The webhook resolves by `customer`, so
    the Customer exists before the session does.
    """
    _require_enabled()
    api = connect.client()

    locked = Organization.objects.select_for_update().get(pk=organization.pk)
    if locked.billing_exempt:
        raise ConflictError(EXEMPT)
    if not locked.is_active and locked.deactivated_for_billing_at is None:
        # Switched off by a person; `apply_coverage` would never turn it
        # back on, so taking the money would be taking it for nothing.
        raise ConflictError(DEACTIVATED_BY_HAND)
    if coverage.has_live_subscription(locked):
        raise ConflictError(ALREADY_SUBSCRIBED)

    if not locked.stripe_customer_id:
        with connect.stripe_errors("create the Stripe customer"):
            customer = api.v1.customers.create(
                {
                    "name": locked.name,
                    "email": locked.email or None,
                    "metadata": {"organization_id": str(locked.pk)},
                }
            )
        locked.stripe_customer_id = customer.id
        locked.save(update_fields=["stripe_customer_id", "updated_at"])
        organization.stripe_customer_id = customer.id
        logger.info(
            "Stripe customer %s created for organization %s by %s",
            customer.id,
            locked.pk,
            getattr(actor, "pk", None),
        )

    metadata = {"organization_id": str(locked.pk)}
    with connect.stripe_errors("open the subscription page"):
        session = api.v1.checkout.sessions.create(
            {
                "mode": "subscription",
                "customer": locked.stripe_customer_id,
                "line_items": [{"price": settings.STRIPE_PLATFORM_PRICE_ID, "quantity": 1}],
                "client_reference_id": str(locked.pk),
                "metadata": metadata,
                "subscription_data": {"metadata": metadata},
                "success_url": connect.settings_page_url(subscription="return"),
                "cancel_url": connect.settings_page_url(subscription="cancelled"),
            }
        )
    return session.url


def portal_url(organization: Organization) -> str:
    """
    Stripe's Customer Portal for this organization: the card, the receipts,
    cancelling and resuming. Its features are dashboard configuration
    (docs/DEPLOY.md); the product has no card pages of its own (ADR-028).
    """
    _require_enabled()
    if not organization.stripe_customer_id:
        raise ConflictError(NO_CUSTOMER)

    with connect.stripe_errors("open the billing portal"):
        session = connect.client().v1.billing_portal.sessions.create(
            {
                "customer": organization.stripe_customer_id,
                "return_url": connect.settings_page_url(),
            }
        )
    return session.url


# ---------------------------------------------------------------------------
# Reading the subscription back
# ---------------------------------------------------------------------------


def refresh_subscription(
    organization: Organization, *, now: dt.datetime | None = None
) -> Organization:
    """
    Re-read the Subscription from Stripe, copy what the coverage rule needs,
    and apply it -- so a payment that comes good reactivates in the same
    call that learned of it.

    Always a fresh read, never from an event's object: Stripe does not
    promise delivery order (4b's lesson). Called on return from Checkout,
    by every platform webhook handler, and by the daily task. A no-op with
    no subscription id, or with Stripe off on the server.
    """
    if not organization.stripe_subscription_id or not connect.enabled():
        return organization

    with connect.stripe_errors("read the subscription"):
        subscription = connect.client().v1.subscriptions.retrieve(
            organization.stripe_subscription_id, {"expand": ["latest_invoice"]}
        )

    customer = getattr(subscription, "customer", None)
    if customer != organization.stripe_customer_id:
        # Not this tenant's subscription. Routing already guarantees it is
        # (the event's customer resolved the organization), so this is the
        # belt to that brace: a misrouted or replayed event mirrors nothing.
        logger.warning(
            "Subscription %s belongs to customer %s, not organization %s's %s; not mirrored",
            organization.stripe_subscription_id,
            customer,
            organization.pk,
            organization.stripe_customer_id,
        )
        return organization

    organization.subscription_status = subscription.status or ""
    organization.subscription_cancel_at_period_end = bool(subscription.cancel_at_period_end)
    organization.subscription_paid_through = paid_through(
        subscription, stored=organization.subscription_paid_through
    )
    organization.save(
        update_fields=[
            "subscription_status",
            "subscription_cancel_at_period_end",
            "subscription_paid_through",
            "updated_at",
        ]
    )
    coverage.apply_coverage(organization, now or timezone.now())
    return organization


def paid_through(subscription, *, stored: dt.datetime | None = None) -> dt.datetime | None:
    """
    The end of the last period Stripe was paid for.

    On the pinned API version the period lives on the subscription *item*,
    not on the Subscription. And a renewal Stripe is still trying to collect
    has already advanced that period, so the item's `current_period_end` is
    the answer only when the latest invoice is paid (or Stripe is running a
    trial of its own); otherwise it is the period's *start*, which is where
    the last paid period ended. Anything else hands a failed card a free
    month.

    An unpaid read never moves the stored date *forward*: a subscription
    left to cycle unpaid (dunning set to "leave as is") advances its period
    every month, and the start of the third unpaid month is not the end of
    the last paid one. `stored` is what the row already says.
    """
    # `.items` on a Stripe object is dict's method: the SDK's documented
    # gotcha, and why this is a subscript.
    items = subscription["items"]
    data = items["data"] if items else []
    if not data:
        return None
    item = data[0]

    latest = subscription.latest_invoice
    paid = subscription.status == "trialing" or getattr(latest, "status", None) == "paid"
    if paid:
        return dt.datetime.fromtimestamp(int(item.current_period_end), tz=dt.UTC)
    start = dt.datetime.fromtimestamp(int(item.current_period_start), tz=dt.UTC)
    return min(start, stored) if stored is not None else start
