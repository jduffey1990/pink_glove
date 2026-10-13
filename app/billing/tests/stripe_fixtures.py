"""
Stripe events for tests, signed the way Stripe signs them.

Tests never reach Stripe. The webhook is fed bytes and a `Stripe-Signature`
header made here, so `stripe.Webhook.construct_event` runs for real with a
secret the test chose; the `stripe` client is patched at the
`billing.connect` boundary for everything else.

Payload shapes follow Stripe's documented objects, trimmed to the fields the
handlers read. Trimming is deliberate: a handler that needs a field these
fixtures lack fails a test before it fails on a real event.
"""

import hashlib
import hmac
import json
import time

from django.conf import settings

#: The signing secret tests hand to the webhook. Not a real one.
WEBHOOK_SECRET = "whsec_test_not_a_real_secret"

#: A connected account id in the shape Stripe uses.
ACCOUNT_ID = "acct_1TestConnectedAcct"


def signature_header(body: bytes, secret: str = WEBHOOK_SECRET, *, timestamp: int | None = None):
    """
    The `Stripe-Signature` header for `body`: `t=<unix>,v1=<hmac-sha256>`
    over `"{t}.{body}"`, which is exactly what the SDK verifies.
    """
    timestamp = int(time.time()) if timestamp is None else timestamp
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def signed(event: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    """The (body, header) pair to POST to the webhook."""
    body = json.dumps(event).encode()
    return body, signature_header(body, secret)


def event(
    type_: str,
    obj: dict,
    *,
    event_id: str = "evt_test_1",
    account: str | None = ACCOUNT_ID,
    created: int | None = None,
) -> dict:
    """
    One Stripe event envelope. `account` is what makes it a Connect event;
    pass `None` for a platform event (Phase 4c).
    """
    envelope = {
        "id": event_id,
        "object": "event",
        "api_version": settings.STRIPE_API_VERSION,
        "created": int(time.time()) if created is None else created,
        "livemode": False,
        "type": type_,
        "data": {"object": obj},
    }
    if account is not None:
        envelope["account"] = account
    return envelope


# --- Platform subscription (Phase 4c) ---------------------------------------
# Events on the platform account carry no `account`; the ledger keys them on
# (event id, ""). Shapes follow the pinned API version, where the billing
# period lives on the subscription *item*, not on the subscription.

#: The signing secret of the platform endpoint. Not a real one, and not the
#: Connect one: a body signed with either must be refused by the other.
PLATFORM_WEBHOOK_SECRET = "whsec_test_platform_not_a_real_secret"

CUSTOMER_ID = "cus_1TestPlatformCustomer"
SUBSCRIPTION_ID = "sub_1TestPlatformSubscription"
PRICE_ID = "price_1TestMonthly"


def platform_event(type_: str, obj: dict, *, event_id: str = "evt_test_platform_1") -> dict:
    """One event on the platform account itself: no `account` key."""
    return event(type_, obj, event_id=event_id, account=None)


def subscription_object(
    *,
    period_start: int,
    period_end: int,
    status: str = "active",
    latest_invoice_status: str | None = "paid",
    cancel_at_period_end: bool = False,
    trial_end: int | None = None,
    customer: str = CUSTOMER_ID,
    subscription_id: str = SUBSCRIPTION_ID,
    organization_id: str = "",
) -> dict:
    """
    A Subscription as `retrieve(expand=["latest_invoice"])` returns it, trimmed
    to what `billing.subscription.refresh_subscription` reads.
    """
    return {
        "id": subscription_id,
        "object": "subscription",
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "trial_end": trial_end,
        "items": {
            "object": "list",
            "data": [
                {
                    "id": "si_test_1",
                    "object": "subscription_item",
                    "current_period_start": period_start,
                    "current_period_end": period_end,
                    "price": {"id": PRICE_ID, "object": "price"},
                }
            ],
        },
        "latest_invoice": None
        if latest_invoice_status is None
        else {"id": "in_test_1", "object": "invoice", "status": latest_invoice_status},
        "metadata": {"organization_id": organization_id},
    }


def invoice_object(*, customer: str = CUSTOMER_ID, status: str = "paid") -> dict:
    """A platform invoice (the subscription's), as `invoice.paid` carries it."""
    return {
        "id": "in_test_1",
        "object": "invoice",
        "customer": customer,
        "status": status,
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": SUBSCRIPTION_ID},
        },
    }


def subscription_checkout_session(
    *,
    customer: str = CUSTOMER_ID,
    subscription: str | None = SUBSCRIPTION_ID,
    organization_id: str = "",
) -> dict:
    """A subscription-mode Checkout Session, as `checkout.session.completed` carries it."""
    return {
        "id": "cs_test_platform_1",
        "object": "checkout.session",
        "mode": "subscription",
        "status": "complete",
        "customer": customer,
        "subscription": subscription,
        "client_reference_id": organization_id,
        "metadata": {"organization_id": organization_id},
    }


def stripe_object(values: dict):
    """
    A dict as the SDK hands it back: a real `StripeObject` tree, so the code
    under test reads `subscription["items"]["data"][0].current_period_end`
    exactly as it would from Stripe -- including the gotcha that `.items`
    on one of these is dict's method, not the field.
    """
    from stripe._util import convert_to_stripe_object

    return convert_to_stripe_object(values, api_key="sk_test_x")
