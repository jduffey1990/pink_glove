"""
Stripe Connect: the one module that talks to Stripe on a tenant's behalf.

Everything here runs against the tenant's *own* Stripe account, reached
through the platform key with `stripe_account=` (ADR-024: Standard accounts,
direct charges, the tenant's own dashboard and payouts). Nothing else in the
codebase imports `stripe`; `billing.webhook` handles what Stripe sends back.

Stripe is optional on the server. With no `STRIPE_SECRET_KEY` every entry
point raises `ServiceUnavailableError` (a 503), the pay link never appears,
and the product is exactly Phase 4a. The `stripe` package is imported here
and only here, so a deployment without a key never loads it at startup.

Tests patch `client()` -- nothing below reaches the network.
"""

import datetime as dt
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from app.exceptions import ConflictError, ServiceUnavailableError
from billing.enums import InvoiceStatus
from billing.models import Invoice
from billing.services import balance_cents
from organizations.models import Organization

logger = logging.getLogger(__name__)

#: Every tenant is a US cleaning company for now (the address country defaults
#: to US). A second currency is a per-organization setting and a migration,
#: not a constant edit.
CURRENCY = "usd"

#: The pay link lives in an email. Terms are 14 to 30 days and a slow payer
#: is the customer the link is for, so it outlives the terms by a wide margin.
PAY_TOKEN_MAX_AGE = dt.timedelta(days=120)
PAY_TOKEN_SALT = "billing.pay"

#: Stripe's minimum. A session is thirty minutes of intent, not a record; the
#: balance is re-read for every new one.
CHECKOUT_SESSION_TTL = dt.timedelta(minutes=30)

NOT_ENABLED = "Card payments are not enabled on this server."


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return bool(settings.STRIPE_ENABLED)


def client():
    """
    A client for the platform account, on the pinned API version.

    Built per call rather than cached: the settings it reads are what tests
    override, and construction is a few attribute assignments.
    """
    if not enabled():
        raise ServiceUnavailableError(NOT_ENABLED)

    import stripe

    return stripe.StripeClient(
        settings.STRIPE_SECRET_KEY, stripe_version=settings.STRIPE_API_VERSION
    )


def _on(organization: Organization) -> dict:
    """Request options that direct a call at the tenant's connected account."""
    return {"stripe_account": organization.stripe_account_id}


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


def _settings_url(query: str) -> str:
    return f"{settings.FRONTEND_BASE_URL}/billing/settings?stripe={query}"


@transaction.atomic
def start_onboarding(organization: Organization, *, actor=None) -> str:
    """
    Begin, or resume, connecting the organization's Stripe account.

    The account is created on the first call and never again; every call
    returns a fresh Account Link, because links expire in minutes and the
    owner may come back a week later to finish the form. Returns the URL to
    send the browser to.
    """
    api = client()

    locked = Organization.objects.select_for_update().get(pk=organization.pk)
    if locked.stripe_charges_enabled:
        raise ConflictError("Stripe is already connected and taking payments.")

    if not locked.stripe_account_id:
        account = api.v1.accounts.create(
            {
                # The Standard account, spelled the way Stripe now asks for it:
                # the tenant sees the full dashboard, pays Stripe's fees
                # itself, and carries its own disputes and negative balance.
                # Stripe collects the onboarding requirements. Every one of
                # these is the choice ADR-024 made; Express would put the
                # losses on the platform.
                "controller": {
                    "stripe_dashboard": {"type": "full"},
                    "fees": {"payer": "account"},
                    "losses": {"payments": "stripe"},
                    "requirement_collection": "stripe",
                },
                "email": locked.email or None,
                "business_profile": {"name": locked.name},
                "metadata": {"organization_id": str(locked.pk)},
            }
        )
        locked.stripe_account_id = account.id
        locked.save(update_fields=["stripe_account_id", "updated_at"])
        organization.stripe_account_id = account.id
        logger.info(
            "Stripe account %s created for organization %s by %s",
            account.id,
            locked.pk,
            getattr(actor, "pk", None),
        )

    link = api.v1.account_links.create(
        {
            "account": locked.stripe_account_id,
            "refresh_url": _settings_url("refresh"),
            "return_url": _settings_url("return"),
            "type": "account_onboarding",
        }
    )
    return link.url


def refresh_account(organization: Organization) -> Organization:
    """
    Copy the account's state from Stripe onto the organization.

    Always a fresh read, never from an event's object: Stripe does not
    promise delivery order, and a stale `account.updated` applied last would
    switch charges off after they came on. `stripe_connected_at` is stamped
    the first time charges are enabled and never moved.
    """
    if not organization.stripe_account_id:
        return organization

    account = client().v1.accounts.retrieve(organization.stripe_account_id)

    organization.stripe_charges_enabled = bool(account.charges_enabled)
    organization.stripe_details_submitted = bool(account.details_submitted)
    if organization.stripe_charges_enabled and organization.stripe_connected_at is None:
        organization.stripe_connected_at = timezone.now()
    organization.save(
        update_fields=[
            "stripe_charges_enabled",
            "stripe_details_submitted",
            "stripe_connected_at",
            "updated_at",
        ]
    )
    return organization


# ---------------------------------------------------------------------------
# The pay link
# ---------------------------------------------------------------------------


def pay_token(invoice: Invoice) -> str:
    """
    A signed token that names the invoice and nothing else.

    It goes in the invoice email. The amounts it leads to are the amounts the
    email already said, and the invoice is the customer's own, so the token
    discloses nothing the email did not.
    """
    return signing.dumps(str(invoice.pk), salt=PAY_TOKEN_SALT)


def invoice_from_pay_token(token: str) -> Invoice | None:
    """The invoice a token names, or None for a bad, expired or stale one."""
    try:
        invoice_id = signing.loads(token, salt=PAY_TOKEN_SALT, max_age=PAY_TOKEN_MAX_AGE)
    except signing.BadSignature:
        return None
    return (
        Invoice.objects.filter(pk=invoice_id)
        .select_related("organization", "customer")
        .prefetch_related("lines")
        .first()
    )


def pay_url(invoice: Invoice) -> str:
    return f"{settings.FRONTEND_BASE_URL}/pay/{pay_token(invoice)}"


def payable(invoice: Invoice) -> str | None:
    """
    Why this invoice cannot be paid by card right now, or None if it can.

    The sentence is the copy the pay page shows (ADR-023: server-owned), and
    the email omits its button on any of them.
    """
    if not enabled():
        return NOT_ENABLED
    organization = invoice.organization
    if not organization.stripe_charges_enabled:
        return f"{organization.name} does not take card payments online."
    if invoice.status != InvoiceStatus.ISSUED:
        return "This invoice is not open for payment."
    if balance_cents(invoice) == 0:
        return "This invoice is paid."
    return None


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


def application_fee_cents(amount_cents: int) -> int:
    """The platform's cut, rounded once, half up. 0 at the shipped setting."""
    percent = Decimal(settings.STRIPE_APPLICATION_FEE_PERCENT)
    if percent <= 0:
        return 0
    return int((Decimal(amount_cents) * percent / 100).quantize(Decimal("1"), ROUND_HALF_UP))


def create_checkout_session(invoice: Invoice) -> str:
    """
    A Stripe-hosted payment page for the invoice's balance, on the tenant's
    account. Returns its URL.

    The invoice id rides on the session *and* on the PaymentIntent: the
    refund and dispute events name the intent and do not inherit the
    session's metadata. Nothing is stored here -- the webhook carries it
    all back, and `provider_reference` on the payment is the intent id.
    """
    # "Not here" (503) before "not now" (409), so a page can tell them apart.
    if not enabled():
        raise ServiceUnavailableError(NOT_ENABLED)
    reason = payable(invoice)
    if reason is not None:
        raise ConflictError(reason)

    organization = invoice.organization
    amount = balance_cents(invoice)
    metadata = {"organization_id": str(organization.pk), "invoice_id": str(invoice.pk)}

    intent_data: dict = {"metadata": metadata}
    fee = application_fee_cents(amount)
    if fee:
        intent_data["application_fee_amount"] = fee

    params: dict = {
        "mode": "payment",
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": CURRENCY,
                    "unit_amount": amount,
                    "product_data": {"name": f"Invoice {invoice.number} from {organization.name}"},
                },
            }
        ],
        "client_reference_id": str(invoice.pk),
        "metadata": metadata,
        "payment_intent_data": intent_data,
        "expires_at": int((timezone.now() + CHECKOUT_SESSION_TTL).timestamp()),
        "success_url": f"{pay_url(invoice)}?paid=1",
        "cancel_url": f"{pay_url(invoice)}?cancelled=1",
    }
    if invoice.bill_to_email:
        params["customer_email"] = invoice.bill_to_email

    session = client().v1.checkout.sessions.create(params, options=_on(organization))
    return session.url


def fee_cents_for(payment_intent_id: str, *, organization: Organization) -> int | None:
    """
    What Stripe kept from a payment, read from the charge's balance
    transaction on the connected account. Never computed from a published
    rate. None when Stripe has not settled it yet, or the read fails -- the
    payment is real either way, so a missing fee is logged, not raised.
    """
    try:
        intent = client().v1.payment_intents.retrieve(
            payment_intent_id,
            {"expand": ["latest_charge.balance_transaction"]},
            options=_on(organization),
        )
        charge = intent.latest_charge
        transaction_ = getattr(charge, "balance_transaction", None) if charge else None
        fee = getattr(transaction_, "fee", None)
        return int(fee) if fee is not None else None
    except Exception:
        logger.warning("Could not read the fee for %s", payment_intent_id, exc_info=True)
        return None
