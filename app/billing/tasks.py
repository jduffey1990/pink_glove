"""
Background work for billing.

Every task takes `organization_id` explicitly (ADR-004, CLAUDE.md invariant 3):
nothing resolved a request in a worker, so nothing set a tenant, and a task
that guessed would eventually guess wrong. The id is also what scopes the
lookup, so a stale or forged invoice id from another tenant finds nothing.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils import timezone

from billing.enums import InvoiceStatus
from billing.models import Invoice
from billing.services import invoice_email_context

logger = logging.getLogger(__name__)


@shared_task(name="billing.send_invoice_email")
def send_invoice_email(organization_id: str, invoice_id: str) -> bool:
    """
    Email one issued invoice to its bill-to address.

    `sent_at` is stamped only after the mail is away, so the field never claims
    a delivery that did not happen. A failure is logged and returns False; the
    dispatcher can press Send again, and nothing about the invoice has changed.
    """
    invoice = (
        Invoice.objects.filter(
            pk=invoice_id, organization_id=organization_id, status=InvoiceStatus.ISSUED
        )
        .select_related("organization", "customer")
        .prefetch_related("lines")
        .first()
    )

    if invoice is None:
        logger.warning(
            "send_invoice_email: no issued invoice %s in organization %s",
            invoice_id,
            organization_id,
        )
        return False

    if not invoice.bill_to_email:
        logger.warning("send_invoice_email: invoice %s has no bill-to address", invoice_id)
        return False

    body = render_to_string("billing/invoice_email.html", invoice_email_context(invoice))
    subject = f"Invoice {invoice.number} from {invoice.organization.name}"

    try:
        message = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [invoice.bill_to_email])
        message.content_subtype = "html"
        message.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to email invoice %s", invoice_id)
        return False

    invoice.sent_at = timezone.now()
    invoice.save(update_fields=["sent_at", "updated_at"])
    return True
