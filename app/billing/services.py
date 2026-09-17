"""
The billing rules. Views stay thin; everything that decides something lives
here, because the seed command, the email task and the API all need the same
answers.

Money discipline, in one place so it is checkable:

* Amounts are integer cents. Rates are `Decimal`. Tax is computed once over the
  whole taxable subtotal and rounded once, half up -- not per line, where the
  rounding error accumulates against whoever has the most lines (ADR-026).
* Nothing recomputes an issued invoice. `issue_invoice` writes the snapshot and
  every later reader takes the numbers off the row.
* Paid is a question about the ledger, never a stored flag (ADR-025).

`ConflictError` means "the record is not in a state where that makes sense"
(409); a Django `ValidationError` means "fix the payload" (400). Both are
rendered by `app.exceptions.exception_handler`, so no view catches either.
"""

import datetime as dt
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from app.exceptions import ConflictError
from billing.enums import InvoiceStatus, LineKind, PaymentState
from billing.models import Invoice, InvoiceLine, InvoiceSequence, Payment
from catalog.enums import PricingModel
from organizations.enums import NoAccessFeeType
from scheduling.enums import JobStatus
from scheduling.models import Job

logger = logging.getLogger(__name__)

#: Statuses a visit can be billed from. A cancelled visit was never delivered;
#: anything still in flight is not finished being delivered.
BILLABLE_STATUSES = (JobStatus.COMPLETE, JobStatus.NO_ACCESS)


# ---------------------------------------------------------------------------
# What is ready to bill
# ---------------------------------------------------------------------------


def live_lines() -> models.QuerySet[InvoiceLine]:
    """
    Invoice lines that still hold a claim on their job.

    Three conditions, and dropping any one of them double-bills or blocks a
    legitimate re-invoice:

    * the line itself is not soft-deleted -- the manager covers this;
    * its invoice is not soft-deleted -- the manager does NOT cover this,
      because a join reaches the related row through `all_objects`;
    * its invoice is not void -- voiding is precisely how a mistaken invoice
      gives its visits back.
    """
    return InvoiceLine.objects.filter(invoice__deleted_at__isnull=True).exclude(
        invoice__status=InvoiceStatus.VOID
    )


def claimed_job_ids() -> models.QuerySet:
    """
    The job ids `live_lines` has a claim on.

    `job_id` is null on an adjustment line, and a NULL inside a `NOT IN`
    subquery makes the whole comparison NULL -- which would quietly empty the
    ready-to-invoice list the moment anyone added a discount. Hence the
    explicit `isnull=False`.
    """
    return live_lines().filter(job__isnull=False).values("job_id")


def billable_jobs(organization, customer=None) -> models.QuerySet[Job]:
    """
    The dispatcher's "ready to invoice" list.

    Completed visits always; no-access visits only where the organization
    actually charges for a locked door, since otherwise every one of them would
    sit in the list forever waiting for a line worth zero.
    """
    statuses = [JobStatus.COMPLETE]
    if (
        organization.no_access_fee_type != NoAccessFeeType.NONE
        and organization.no_access_fee_value > 0
    ):
        statuses.append(JobStatus.NO_ACCESS)

    jobs = (
        Job.objects.filter(organization=organization, status__in=statuses)
        .exclude(pk__in=claimed_job_ids())
        .select_related("customer", "location", "service")
        .order_by("scheduled_start")
    )

    if customer is not None:
        jobs = jobs.filter(customer=customer)

    return jobs


# ---------------------------------------------------------------------------
# Building a draft
# ---------------------------------------------------------------------------


def line_description(job: Job) -> str:
    """
    What the customer reads: the service, and the day it happened, in their
    own organization's timezone.

    A snapshot. Renaming the service next month must not rewrite this.
    """
    local_day = job.scheduled_start.astimezone(job.organization.tz)
    return f"{job.service.name} -- {local_day:%a %-d %b %Y}"


def billable_amount_cents(job: Job) -> int:
    """
    What this visit will put on an invoice.

    A completed visit bills the price agreed when it was scheduled -- never a
    re-quote at today's prices (ADR-026). A no-access visit bills the
    organization's fee, which may be nothing.

    The ready-to-invoice list and `draft_invoice` both read this, so what a
    dispatcher is shown before pressing the button is what lands on the line.
    """
    if job.status == JobStatus.NO_ACCESS:
        return job.organization.no_access_fee_cents(job.price_cents)
    return job.price_cents


@transaction.atomic
def draft_invoice(*, customer, jobs, actor=None) -> Invoice:
    """
    Open a draft invoice covering `jobs` for `customer`.

    The jobs are locked for the length of the transaction, which is what makes
    "a job appears on at most one live invoice" hold under two dispatchers
    invoicing the same week at once. The check cannot be a database constraint:
    a partial index cannot see the status of the invoice a line hangs off, and
    a voided invoice must give its visits back.
    """
    organization = customer.organization
    job_ids = [getattr(job, "pk", job) for job in jobs]

    if not job_ids:
        raise ValidationError({"jobs": "An invoice needs at least one visit."})

    # Locked, and re-read inside the lock: what was billable when the page
    # rendered is not necessarily billable now.
    locked = list(
        Job.objects.select_for_update()
        .filter(pk__in=job_ids, organization=organization)
        .select_related("service", "organization")
    )
    found = {job.pk for job in locked}

    missing = [job_id for job_id in job_ids if job_id not in found]
    if missing:
        raise ValidationError({"jobs": "One of those visits does not exist here."})

    for job in locked:
        if job.customer_id != customer.pk:
            raise ValidationError({"jobs": "One of those visits belongs to another customer."})
        if job.status not in BILLABLE_STATUSES:
            raise ValidationError(
                {"jobs": f"{line_description(job)} is {job.get_status_display().lower()}."}
            )

    already = set(live_lines().filter(job_id__in=found).values_list("job_id", flat=True))
    if already:
        raise ConflictError(
            "One of those visits is already on an invoice.",
            {"jobs": [str(job_id) for job_id in already]},
        )

    invoice = Invoice.objects.create(organization=organization, customer=customer)

    lines = []
    for position, job in enumerate(sorted(locked, key=lambda j: j.scheduled_start)):
        if job.status == JobStatus.COMPLETE:
            lines.append(
                InvoiceLine(
                    organization=organization,
                    invoice=invoice,
                    job=job,
                    kind=LineKind.VISIT,
                    description=line_description(job),
                    amount_cents=job.price_cents,
                    is_taxable=job.service.is_taxable,
                    position=position,
                )
            )
            continue

        fee = billable_amount_cents(job)
        if fee <= 0:
            # No line at all rather than a $0.00 row explaining that this
            # organization does not charge for a locked door.
            continue
        lines.append(
            InvoiceLine(
                organization=organization,
                invoice=invoice,
                job=job,
                kind=LineKind.NO_ACCESS_FEE,
                description=f"No access -- {line_description(job)}",
                amount_cents=fee,
                is_taxable=job.service.is_taxable,
                position=position,
            )
        )

    if not lines:
        raise ValidationError(
            {"jobs": "Nothing on that selection is chargeable, so there is nothing to invoice."}
        )

    for line in lines:
        line.save()

    return invoice


@transaction.atomic
def reprice_line_from_time_worked(line: InvoiceLine) -> InvoiceLine:
    """
    Re-price an hourly visit from the hours the crew actually recorded.

    Never automatic (ADR-026): a crew running long is not self-evidently the
    customer's bill, so a person decides. Open time entries are ignored --
    someone still clocked in has not worked a knowable number of hours.

    Floors at the service's minimum charge, exactly as `quote_cents` does, so
    a five-minute visit still bills the callout.
    """
    assert_draft(line.invoice, "re-price a line on")

    if line.kind != LineKind.VISIT or line.job_id is None:
        raise ConflictError("Only a visit line can be re-priced from time worked.")

    service = line.job.service
    if service.pricing_model != PricingModel.HOURLY:
        raise ConflictError(
            f"{service.name} is not priced hourly, so there are no hours to re-price from."
        )

    minutes = 0
    for entry in line.job.time_entries.filter(clock_out__isnull=False):
        minutes += entry.duration_minutes or 0

    if minutes <= 0:
        raise ConflictError("Nobody has recorded finished time on that visit yet.")

    line.amount_cents = service.quote_cents(hours=Decimal(minutes) / Decimal(60))
    line.save(update_fields=["amount_cents", "updated_at"])
    return line


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def effective_tax_rate(invoice: Invoice) -> Decimal:
    """
    The rate this invoice is reckoned at.

    A draft follows the organization, so a rate corrected before issue takes
    effect. Anything issued follows its own snapshot for good (ADR-026).
    """
    if invoice.status == InvoiceStatus.DRAFT:
        return Decimal(invoice.organization.tax_rate_percent)
    return Decimal(invoice.tax_rate_percent)


def totals(invoice: Invoice) -> dict[str, int]:
    """
    Subtotal, tax and total in cents, from the invoice's live lines.

    Tax is computed over the taxable subtotal as a whole and rounded once. Per
    line it would round up to six times on a six-line invoice, which is both
    wrong and, to the one person who checks, obviously wrong.

    A discount large enough to take the taxable subtotal negative floors the
    tax at zero rather than generating a credit the tax authority never issued.
    """
    lines = list(invoice.lines.all())

    subtotal = sum(line.amount_cents for line in lines)
    taxable = sum(line.amount_cents for line in lines if line.is_taxable)

    rate = effective_tax_rate(invoice)
    if taxable <= 0 or rate <= 0:
        tax = 0
    else:
        raw = Decimal(taxable) * rate / Decimal(100)
        tax = int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return {"subtotal_cents": subtotal, "tax_cents": tax, "total_cents": subtotal + tax}


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def live_payments(invoice: Invoice) -> models.QuerySet[Payment]:
    """Payments that still count: not voided, not soft-deleted."""
    return invoice.payments.filter(voided_at__isnull=True)


def paid_cents(invoice: Invoice) -> int:
    """
    What has been paid toward the invoice. Tips are excluded on purpose: a tip
    is not revenue and never settles a bill (ADR-025).
    """
    return live_payments(invoice).aggregate(total=models.Sum("amount_cents"))["total"] or 0


def balance_cents(invoice: Invoice) -> int:
    """What is still owed. Never negative -- an overpayment is not a debt."""
    if invoice.status != InvoiceStatus.ISSUED:
        return 0
    return max(invoice.total_cents - paid_cents(invoice), 0)


def payment_state(invoice: Invoice) -> str:
    """UNPAID, PARTIAL or PAID, derived from the ledger every time."""
    if invoice.status != InvoiceStatus.ISSUED:
        return PaymentState.UNPAID

    paid = paid_cents(invoice)
    if paid <= 0:
        return PaymentState.UNPAID
    if paid >= invoice.total_cents:
        return PaymentState.PAID
    return PaymentState.PARTIAL


def is_overdue(invoice: Invoice, *, today: dt.date | None = None) -> bool:
    """
    Past its due date and still owed something, reckoned in the organization's
    own timezone. A paid invoice is never overdue, whatever the date says.
    """
    if invoice.status != InvoiceStatus.ISSUED or invoice.due_on is None:
        return False
    if payment_state(invoice) == PaymentState.PAID:
        return False

    return invoice.due_on < (today or invoice.organization.today())


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def assert_draft(invoice: Invoice, verb: str) -> None:
    """Refuse anything that would edit a document already sent (ADR-026)."""
    if invoice.status != InvoiceStatus.DRAFT:
        raise ConflictError(
            f"This invoice is {invoice.get_status_display().lower()}, so you cannot {verb} it. "
            "Corrections after issue are a void and a new invoice.",
            {"status": invoice.status},
        )


@transaction.atomic
def issue_invoice(invoice: Invoice, *, actor=None, today: dt.date | None = None) -> Invoice:
    """
    Number the invoice and freeze everything it says.

    The number comes off `InvoiceSequence` under `select_for_update`, so two
    dispatchers issuing at the same instant get consecutive numbers rather than
    the same one and a unique-constraint error.
    """
    assert_draft(invoice, "issue")

    amounts = totals(invoice)
    if not invoice.lines.exists():
        raise ConflictError(
            "An invoice with no lines cannot be issued.", {"status": invoice.status}
        )
    if amounts["total_cents"] <= 0:
        raise ConflictError(
            "An invoice has to come to more than nothing. Adjust the lines, or delete the draft.",
            {"status": invoice.status, **amounts},
        )

    organization = invoice.organization
    customer = invoice.customer

    sequence, _ = InvoiceSequence.objects.get_or_create(organization=organization)
    sequence = InvoiceSequence.objects.select_for_update().get(pk=sequence.pk)

    invoice.number = f"{organization.invoice_prefix}-{sequence.next_number:04d}"
    sequence.next_number += 1
    sequence.save(update_fields=["next_number", "updated_at"])

    issued_on = today or organization.today()

    invoice.status = InvoiceStatus.ISSUED
    invoice.issued_on = issued_on
    invoice.due_on = issued_on + dt.timedelta(days=organization.invoice_terms_days)
    invoice.tax_rate_percent = Decimal(organization.tax_rate_percent)
    invoice.subtotal_cents = amounts["subtotal_cents"]
    invoice.tax_cents = amounts["tax_cents"]
    invoice.total_cents = amounts["total_cents"]
    invoice.bill_to_name = customer.display_name
    invoice.bill_to_email = customer.email
    invoice.bill_to_address = billing_address(customer)

    invoice.save(
        update_fields=[
            "number",
            "status",
            "issued_on",
            "due_on",
            "tax_rate_percent",
            "subtotal_cents",
            "tax_cents",
            "total_cents",
            "bill_to_name",
            "bill_to_email",
            "bill_to_address",
            "updated_at",
        ]
    )
    return invoice


def billing_address(customer) -> str:
    """The customer's billing address as it should print, one line each."""
    parts = [
        customer.billing_line1,
        customer.billing_line2,
        ", ".join(part for part in (customer.billing_city, customer.billing_state) if part),
        customer.billing_postal_code,
    ]
    return "\n".join(part for part in parts if part)


@transaction.atomic
def void_invoice(invoice: Invoice, *, actor=None, reason: str) -> Invoice:
    """
    Void an issued invoice, freeing its visits to be billed again.

    Refused while live payments point at it: money must never point at a void
    document. Void the payments first, which records why each one went.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Say why this invoice is being voided."})

    if invoice.status == InvoiceStatus.DRAFT:
        raise ConflictError(
            "A draft has not been issued, so there is nothing to void. Delete it instead.",
            {"status": invoice.status},
        )
    if invoice.status == InvoiceStatus.VOID:
        raise ConflictError("This invoice is already void.", {"status": invoice.status})

    if live_payments(invoice).exists():
        raise ConflictError(
            "This invoice has payments against it. Void those first, with a reason "
            "for each, so no money points at a void document.",
            {"status": invoice.status},
        )

    invoice.status = InvoiceStatus.VOID
    invoice.voided_at = timezone.now()
    invoice.voided_by = actor
    invoice.void_reason = reason[:255]
    invoice.save(update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"])
    return invoice


@transaction.atomic
def record_payment(
    invoice: Invoice,
    *,
    method: str,
    amount_cents: int,
    received_on: dt.date,
    tip_cents: int = 0,
    reference: str = "",
    actor=None,
    provider: str = "",
    provider_reference: str = "",
) -> Payment:
    """
    Write a payment against an issued invoice.

    `amount_cents` may not exceed the balance. An excess is either a tip or a
    mistake, and the caller has to say which -- guessing would either pocket a
    tip into revenue or silently overpay an invoice.
    """
    if invoice.status != InvoiceStatus.ISSUED:
        raise ConflictError(
            f"A {invoice.get_status_display().lower()} invoice cannot take a payment.",
            {"status": invoice.status},
        )

    if amount_cents <= 0:
        raise ValidationError({"amount_cents": "A payment has to be for more than nothing."})
    if tip_cents < 0:
        raise ValidationError({"tip_cents": "A tip cannot be negative."})

    # Locked so two people recording the same check cannot both see a balance.
    locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
    balance = balance_cents(locked)

    if amount_cents > balance:
        raise ValidationError(
            {
                "amount_cents": (
                    f"That is more than the {balance} cents still owed. If the extra "
                    "is a tip, put it in tip_cents."
                )
            }
        )

    return Payment.objects.create(
        organization=invoice.organization,
        invoice=invoice,
        method=method,
        amount_cents=amount_cents,
        tip_cents=tip_cents,
        received_on=received_on,
        reference=reference,
        recorded_by=actor,
        provider=provider,
        provider_reference=provider_reference,
    )


@transaction.atomic
def void_payment(payment: Payment, *, actor=None, reason: str) -> Payment:
    """
    Void a payment. The row stays; the balance comes back.

    Never an edit and never a delete: "did they pay me?" is the dispute this
    table exists to settle, and a table its own subject can amend settles
    nothing (ADR-025, same reasoning as the reveal trail in ADR-016).
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Say why this payment is being voided."})

    if payment.is_void:
        raise ConflictError("This payment is already void.")

    payment.voided_at = timezone.now()
    payment.voided_by = actor
    payment.void_reason = reason[:255]
    payment.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_at"])
    return payment


# ---------------------------------------------------------------------------
# What the caller may do next
# ---------------------------------------------------------------------------

#: Every action the invoice API offers, so the pages draw their buttons from
#: the server's answer rather than a copy of these rules (ADR-023, the same
#: shape as `Job.next_statuses`).
INVOICE_ACTIONS = ("edit", "delete", "issue", "void", "send", "record_payment")


def available_actions(invoice: Invoice) -> list[str]:
    """Which of `INVOICE_ACTIONS` this invoice will accept right now."""
    if invoice.status == InvoiceStatus.DRAFT:
        actions = ["edit", "delete"]
        if invoice.lines.exists() and totals(invoice)["total_cents"] > 0:
            actions.append("issue")
        return actions

    if invoice.status == InvoiceStatus.ISSUED:
        actions = ["send"]
        if balance_cents(invoice) > 0:
            actions.append("record_payment")
        if not live_payments(invoice).exists():
            actions.append("void")
        return actions

    return []


# ---------------------------------------------------------------------------
# Sending it
# ---------------------------------------------------------------------------


def format_cents(cents: int) -> str:
    """
    Cents as dollars, for display only.

    Formatting lives here rather than in the template so that the one place
    cents become dollars is the same for the email, the seed output and any
    later PDF. Negative amounts print as -$25.00, not $-25.00.
    """
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def invoice_email_context(invoice: Invoice) -> dict:
    """
    Everything the invoice email prints, already formatted.

    Amounts come off the snapshot, never recomputed -- what the customer reads
    in the email is what the invoice says (ADR-026). The balance is the one
    live number, because payments arrive after the invoice goes out.
    """
    balance = balance_cents(invoice)

    return {
        "organization": invoice.organization,
        "invoice": invoice,
        "lines": [
            {"description": line.description, "amount": format_cents(line.amount_cents)}
            for line in invoice.lines.all()
        ],
        "subtotal": format_cents(invoice.subtotal_cents),
        "tax": format_cents(invoice.tax_cents),
        "tax_rate": invoice.tax_rate_percent,
        "total": format_cents(invoice.total_cents),
        "paid": format_cents(paid_cents(invoice)),
        "balance": format_cents(balance),
        "is_settled": balance == 0,
        "footer": invoice.organization.invoice_footer,
    }


def send_invoice(invoice: Invoice, *, actor=None) -> Invoice:
    """
    Queue the invoice email.

    Validates here and delivers in a worker: a dispatcher pressing Send should
    not wait on an SMTP handshake, and a bounced connection should not lose
    them the invoice. `sent_at` is stamped by the task once the mail is
    actually away, so the field never claims something that did not happen.
    """
    from billing.tasks import send_invoice_email

    if invoice.status != InvoiceStatus.ISSUED:
        raise ConflictError(
            f"A {invoice.get_status_display().lower()} invoice cannot be sent. Issue it first.",
            {"status": invoice.status},
        )

    if not invoice.bill_to_email:
        raise ValidationError(
            {
                "bill_to_email": (
                    "This invoice has no email address on it. Add one to the "
                    "customer and issue a fresh invoice."
                )
            }
        )

    send_invoice_email.delay(str(invoice.organization_id), str(invoice.pk))
    return invoice
