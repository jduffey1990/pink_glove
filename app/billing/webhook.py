"""
The Connect webhook: what Stripe tells us about a tenant's account.

The shape is *verify, ledger, resolve, enqueue, 200* -- everything that can
fail for a business reason happens in a worker, so Stripe's thirty-second
clock is never near and a handler bug never turns into a retry storm at the
edge. In the request, and in one transaction:

1. The event is written to `StripeEvent` on (event id, account). If it is
   already there, Stripe is retrying and the first delivery owns it: 200.
2. `event.account` is resolved to the organization with that
   `stripe_account_id` -- before anything is enqueued, so the task takes
   `organization_id` like every other (invariant 3). No owner: the event is
   ledgered IGNORED and dropped, never guessed at (ADR-024).
3. A type with no handler is IGNORED too. Only what is handled is registered.
4. The task is handed ids only. The payload stays in the ledger row, which is
   what makes a FAILED event replayable without asking Stripe to resend.

The view is a plain Django view, outside DRF and the schema: nothing in
`ui/` calls it, and `request.body` has to reach `construct_event` as the
bytes Stripe signed. The secret is read per request, never at import, so a
test can override it and Phase 4c can add the platform endpoint with its own.

Handlers are idempotent because both ledgers are: the event row refuses a
second delivery, and `Payment`'s unique (provider, provider_reference) refuses
a second row for one PaymentIntent.
"""

import datetime as dt
import json
import logging
from collections.abc import Callable

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from billing import connect
from billing.enums import StripeEventStatus
from billing.models import Invoice, Payment, StripeEvent
from billing.services import (
    format_cents,
    overpaid_cents,
    record_provider_payment,
    void_payment,
)
from organizations.models import Organization

logger = logging.getLogger(__name__)

PROVIDER = "stripe"
NO_HANDLER = "No handler for this event type."


class HandlerError(Exception):
    """The event is well-formed but cannot be applied. The row goes FAILED."""


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------


def receive(event: dict) -> StripeEvent | None:
    """
    Ledger one verified event and, if anything is to be done, enqueue it.

    Returns the ledger row, or None when this delivery was a retry of one
    already ledgered. Never raises for a business reason: an event nobody
    owns or nobody handles is a 200 with an IGNORED row, because a non-200
    would only make Stripe send it again.
    """
    account = event.get("account") or ""
    organization = (
        Organization.objects.filter(stripe_account_id=account).first() if account else None
    )
    handled = event["type"] in HANDLERS

    if organization is None:
        status, error = StripeEventStatus.IGNORED, f"No organization owns account {account!r}."
    elif not handled:
        status, error = StripeEventStatus.IGNORED, NO_HANDLER
    else:
        status, error = StripeEventStatus.RECEIVED, ""

    try:
        with transaction.atomic():
            row = StripeEvent.objects.create(
                event_id=event["id"],
                account=account,
                type=event["type"],
                organization=organization,
                payload=event,
                status=status,
                error=error,
            )
    except IntegrityError:
        return _redeliver(event["id"], account)

    if status == StripeEventStatus.RECEIVED:
        _enqueue(row)
    else:
        logger.info("Stripe event %s (%s) ignored: %s", event["id"], event["type"], error)
    return row


def _enqueue(row: StripeEvent) -> None:
    from billing.tasks import process_stripe_event

    process_stripe_event.delay(str(row.organization_id), str(row.pk))


def _redeliver(event_id: str, account: str) -> None:
    """
    The same event again. Stripe retries on its own, and an operator can
    press Resend; either way the first delivery owns the row. A row that is
    PROCESSED or IGNORED is left alone. One still RECEIVED or FAILED is
    re-queued -- that is what Resend is *for* after a worker died under the
    task, and it is safe because both ledgers make a second run a no-op.
    """
    row = StripeEvent.objects.filter(event_id=event_id, account=account).first()
    if (
        row is not None
        and row.organization_id is not None
        and row.status in (StripeEventStatus.RECEIVED, StripeEventStatus.FAILED)
    ):
        logger.info("Stripe event %s delivered again while %s; re-queued", event_id, row.status)
        _enqueue(row)
    else:
        logger.info("Stripe event %s for %s delivered again; ignored", event_id, account)
    return None


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        secret = settings.STRIPE_CONNECT_WEBHOOK_SECRET
        if not settings.STRIPE_ENABLED or not secret:
            return JsonResponse({"detail": connect.NOT_ENABLED}, status=503)

        import stripe

        try:
            stripe.Webhook.construct_event(
                request.body, request.headers.get("Stripe-Signature", ""), secret
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            # The body is not logged: unverified, it is whatever the sender
            # wanted us to write down.
            logger.warning("Stripe webhook rejected: %s", type(exc).__name__)
            return HttpResponse(status=400)

        # The bytes that were signed, as sent -- not the SDK's object model,
        # so the ledger holds exactly what Stripe delivered.
        event = json.loads(request.body)
        if not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
            # Signed, but not an event. A 400 rather than a 500: Stripe would
            # retry a 500 for days, and cannot usefully retry a body it could
            # not form.
            logger.warning("Stripe webhook rejected: no event id or type")
            return HttpResponse(status=400)

        receive(event)
        return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# Processing, in the worker
# ---------------------------------------------------------------------------


def process(organization_id: str, stripe_event_id: str) -> str:
    """
    Apply one ledgered event. Returns the status it ended in.

    The handler and the PROCESSED mark share a transaction, so a failure
    rolls back everything the handler wrote; the FAILED mark is written
    after, outside it, and the exception is re-raised for the task's retry.
    An event already PROCESSED or IGNORED is left alone -- a replay is a
    no-op at this layer as well as in the handlers.
    """
    row = StripeEvent.objects.filter(pk=stripe_event_id, organization_id=organization_id).first()
    if row is None:
        logger.warning("Stripe event %s not in organization %s", stripe_event_id, organization_id)
        return StripeEventStatus.IGNORED
    if row.status not in (StripeEventStatus.RECEIVED, StripeEventStatus.FAILED):
        return row.status

    handler = HANDLERS.get(row.type)
    if handler is None:
        _finish(row, StripeEventStatus.IGNORED, NO_HANDLER)
        return row.status

    try:
        with transaction.atomic():
            handler(row)
            _finish(row, StripeEventStatus.PROCESSED)
    except Exception as exc:
        _finish(row, StripeEventStatus.FAILED, f"{type(exc).__name__}: {exc}"[:2000])
        logger.exception("Stripe event %s (%s) failed", row.event_id, row.type)
        raise
    return row.status


# A row still RECEIVED this long after delivery has lost its task: the
# handler runs in milliseconds and the task's three retries with backoff are
# over inside a minute, so this is a margin, not a tuning knob. Re-queuing is
# idempotent, so guessing short costs a wasted run, never a second payment.
STALE_AFTER = dt.timedelta(minutes=10)
# ...and this long means re-queuing is not helping: the message is taken and
# never finished -- a handler the kernel kills every time, say. FAILED puts it
# in the admin's queue for a person instead of killing a worker every sweep.
ABANDON_AFTER = dt.timedelta(hours=1)
ABANDONED = "Not processed within an hour of delivery despite re-queuing; needs a person."


def sweep() -> dict[str, int]:
    """
    The ledger as the queue of record (ADR-024): re-queue every event that
    was delivered, ledgered, and then never applied.

    Late acknowledgement puts a task back when a worker *child* dies under
    it; nothing does when the whole worker machine goes, or the broker
    forgets -- with a Redis broker an unacknowledged message comes back only
    after the visibility timeout, an hour by default. The first staging
    payment sat RECEIVED for a day this way (2026-09-22). This runs from
    beat every few minutes and gets there in ten, from the row.

    Takes no tenant: it fans out per row, and each task is handed that row's
    own organization id (invariant 3), the same shape as
    `audit.evaluate_access_reveals_all`.
    """
    now = timezone.now()
    stale = StripeEvent.objects.filter(
        status=StripeEventStatus.RECEIVED,
        organization__isnull=False,
        created_at__lt=now - STALE_AFTER,
    ).order_by("created_at")

    requeued = abandoned = 0
    for row in stale:
        if row.created_at < now - ABANDON_AFTER:
            _finish(row, StripeEventStatus.FAILED, ABANDONED)
            logger.error("Stripe event %s (%s) abandoned: %s", row.event_id, row.type, ABANDONED)
            abandoned += 1
        else:
            logger.warning(
                "Stripe event %s (%s) still RECEIVED after %s; re-queued",
                row.event_id,
                row.type,
                now - row.created_at,
            )
            _enqueue(row)
            requeued += 1
    return {"requeued": requeued, "abandoned": abandoned}


def _finish(row: StripeEvent, status: str, error: str = "") -> None:
    row.status = status
    row.error = error
    row.processed_at = timezone.now()
    row.save(update_fields=["status", "error", "processed_at", "updated_at"])


def _object(row: StripeEvent) -> dict:
    return row.payload["data"]["object"]


def _stripe_payments_for(organization: Organization, payment_intent: str):
    """
    The live rows this PaymentIntent has produced: the original, or the
    replacement a partial refund wrote (`pi_x:refunded-N`).
    """
    return Payment.objects.filter(
        organization=organization, provider=PROVIDER, voided_at__isnull=True
    ).filter(
        Q(provider_reference=payment_intent)
        | Q(provider_reference__startswith=f"{payment_intent}:")
    )


def _refunded_in(provider_reference: str) -> int:
    """The cumulative refund a replacement reference records; 0 for an original."""
    _, sep, tail = provider_reference.partition(":refunded-")
    return int(tail) if sep and tail.isdigit() else 0


# --- checkout.session.completed ---------------------------------------------


def checkout_session_completed(row: StripeEvent) -> None:
    """
    The customer paid. One `Payment`, provider reference the PaymentIntent.

    The invoice is looked up *within* the organization the account resolved
    to. Metadata naming another tenant's invoice -- forged or a bug -- is a
    FAILED event, not a payment on somebody else's book.
    """
    session = _object(row)
    if session.get("payment_status") != "paid":
        # An asynchronous method still settling; nothing has been paid yet.
        return

    organization = row.organization
    invoice_id = (session.get("metadata") or {}).get("invoice_id") or session.get(
        "client_reference_id"
    )
    invoice = Invoice.objects.filter(pk=invoice_id, organization=organization).first()
    if invoice is None:
        raise HandlerError(f"Invoice {invoice_id!r} is not in organization {organization.pk}.")

    intent = session.get("payment_intent")
    if not intent:
        raise HandlerError("The session names no PaymentIntent.")

    payment = record_provider_payment(
        invoice,
        provider=PROVIDER,
        provider_reference=intent,
        amount_cents=int(session["amount_total"]),
        received_on=organization.today(),
        reference=session["id"],
    )
    if payment.fee_cents is None:
        fee = connect.fee_cents_for(intent, organization=organization)
        if fee is not None:
            payment.fee_cents = fee
            payment.save(update_fields=["fee_cents", "updated_at"])

    over = overpaid_cents(invoice)
    if over:
        logger.warning("Invoice %s overpaid by %s cents by card", invoice.number, over)


# --- charge.refunded -------------------------------------------------------


def charge_refunded(row: StripeEvent) -> None:
    """
    Money went back. The ledger cannot shrink a row (ADR-025), so the
    payment is voided with the reason and, for a partial refund, a new
    payment for what remains is written under `pi:refunded-<cumulative>` --
    a reference that repeats for a replay and changes for a further refund.
    """
    charge = _object(row)
    intent = charge.get("payment_intent")
    if not intent:
        return
    organization = row.organization

    refunded = int(charge.get("amount_refunded") or 0)
    net = int(charge["amount"]) - refunded
    replacement = f"{intent}:refunded-{refunded}"

    live = list(_stripe_payments_for(organization, intent))
    if not live:
        # Nothing of ours to reverse. Not an error -- Stripe may be telling
        # us about a charge that was never ours to ledger -- but loud, because
        # the other way this happens is a checkout event lost before it was
        # applied (a worker killed under it): the operator sees a refund that
        # changed nothing, and the fix is to replay the checkout event first
        # (docs/DEPLOY.md, "When a payment does not show up").
        logger.warning(
            "Stripe refund %s on %s: no live payment for PaymentIntent %s in organization %s;"
            " nothing to void",
            charge.get("id"),
            row.account,
            intent,
            organization.pk,
        )
        return

    # Stripe does not promise order. The cumulative amount already applied
    # is in the live replacement's reference; an event that refunds no more
    # than that is a replay or a straggler, and applying it would put money
    # back on the invoice that has already left.
    applied = max((_refunded_in(p.provider_reference) for p in live), default=0)
    if refunded <= applied:
        return

    reason = f"Refunded in Stripe: {format_cents(refunded)} of {format_cents(charge['amount'])}"
    for payment in live:
        void_payment(payment, reason=reason)

    if net > 0:
        original = live[0]
        record_provider_payment(
            original.invoice,
            provider=PROVIDER,
            provider_reference=replacement,
            amount_cents=net,
            received_on=original.received_on,
            reference=charge["id"],
            fee_cents=original.fee_cents,
        )


# --- charge.dispute.* ------------------------------------------------------


def charge_dispute_created(row: StripeEvent) -> None:
    """The money is held, not gone: annotate, and leave the balance alone."""
    dispute = _object(row)
    now = timezone.now()
    for payment in _stripe_payments_for(row.organization, dispute.get("payment_intent") or ""):
        if payment.disputed_at is None:
            payment.disputed_at = now
        payment.dispute_status = dispute.get("status") or ""
        payment.save(update_fields=["disputed_at", "dispute_status", "updated_at"])


def charge_dispute_closed(row: StripeEvent) -> None:
    dispute = _object(row)
    status = dispute.get("status") or ""
    for payment in _stripe_payments_for(row.organization, dispute.get("payment_intent") or ""):
        payment.dispute_status = status
        payment.save(update_fields=["dispute_status", "updated_at"])
        if status == "lost":
            void_payment(payment, reason=f"Dispute lost: {dispute['id']}")


# --- account.updated -------------------------------------------------------


def account_updated(row: StripeEvent) -> None:
    """Re-read, never copy: Stripe does not promise order."""
    connect.refresh_account(row.organization)


HANDLERS: dict[str, Callable[[StripeEvent], None]] = {
    "checkout.session.completed": checkout_session_completed,
    "charge.refunded": charge_refunded,
    "charge.dispute.created": charge_dispute_created,
    "charge.dispute.closed": charge_dispute_closed,
    "account.updated": account_updated,
}
