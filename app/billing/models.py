"""
Invoices, their lines, and the payment ledger.

Three ideas hold this app together, and each is easy to "simplify" wrongly:

* **An invoice is a snapshot** (ADR-026). Every amount, the tax rate, and the
  bill-to name and address are written when it is issued and never recomputed.
  Raising a price, moving a customer or changing the tax rate must not alter a
  document that has already been sent.
* **Paid is derived, never stored** (ADR-025). An invoice is paid when its
  live payments sum to its total. `InvoiceStatus` says draft, issued or void
  and nothing else; `billing.services.payment_state` answers the rest.
* **Nothing issued is edited.** Corrections are a void and a new invoice, so a
  number is never reused and the sequence has no silent rewrites. A payment is
  voided with a reason, never amended -- "did they pay me?" is the dispute
  this table exists to settle.

The rule that one job may be billed only once is *not* a database constraint.
A partial unique index cannot see whether the invoice a line hangs off has
been voided, and a voided invoice must free its jobs. It lives in
`billing.services` under `select_for_update`.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from base.models import TenantModel
from billing.enums import InvoiceStatus, LineKind, PaymentMethod
from customers.models import Customer
from scheduling.models import Job
from users.models import CustomUser


class Invoice(TenantModel):
    """One bill to one customer, covering any number of visits."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="invoices")

    #: Empty until issued, then taken from `InvoiceSequence` under a row lock
    #: so two dispatchers issuing at the same moment get consecutive numbers
    #: rather than the same one. Empty rather than null because every other
    #: CharField here reads that way and `ruff`'s DJ001 enforces it; the
    #: unique constraint below is partial on `number != ""` in consequence.
    number = models.CharField(max_length=32, blank=True, default="", editable=False)

    status = models.CharField(
        max_length=16, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT, db_index=True
    )

    #: Organization-local dates, not UTC instants. An invoice issued at 6pm in
    #: Denver is dated that day, not the next one.
    issued_on = models.DateField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)

    # --- Snapshot, written at issue, never recomputed (ADR-026) -------------

    bill_to_name = models.CharField(max_length=255, blank=True, default="")
    bill_to_email = models.EmailField(blank=True, default="")
    bill_to_address = models.TextField(blank=True, default="")

    tax_rate_percent = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    subtotal_cents = models.PositiveIntegerField(default=0)
    tax_cents = models.PositiveIntegerField(default=0)
    total_cents = models.PositiveIntegerField(default=0)

    #: Shown to the customer on the invoice itself.
    notes = models.TextField(blank=True, default="")

    #: Who opened the draft, and who issued it. Usually the same person, not
    #: always: a dispatcher prepares the month and an owner signs it off.
    #: SET_NULL because a document outlives the employment of whoever made it.
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    issued_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    void_reason = models.CharField(max_length=255, blank=True, default="")

    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        verbose_name = "Invoice"
        verbose_name_plural = "Invoices"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"],
                condition=~models.Q(number="") & models.Q(deleted_at__isnull=True),
                name="unique_invoice_number_per_organization",
            )
        ]
        indexes = [
            *TenantModel.Meta.indexes,
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "customer"]),
            models.Index(fields=["organization", "due_on"]),
        ]

    def __str__(self):
        return self.number or f"Draft invoice for {self.customer_id}"

    @property
    def is_draft(self) -> bool:
        return self.status == InvoiceStatus.DRAFT

    def delete(self, *args, **kwargs):
        """
        A draft is a working document and may be thrown away. Anything issued
        is a document someone has seen, so it is voided instead -- with a
        reason, keeping its number spent.
        """
        if not self.is_draft:
            raise ValidationError(
                {
                    "detail": (
                        "An issued invoice cannot be deleted. Void it instead, which "
                        "keeps its number and records why."
                    )
                }
            )
        super().delete(*args, **kwargs)


class InvoiceLine(TenantModel):
    """
    One row on an invoice.

    `description` and `is_taxable` are snapshots too: the service may be
    renamed or made taxable next month, and this line says what it said when
    the customer read it.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    kind = models.CharField(max_length=16, choices=LineKind.choices)

    #: The visit being billed. Null only for an adjustment, which is somebody
    #: writing a line by hand.
    job = models.ForeignKey(
        Job, on_delete=models.PROTECT, null=True, blank=True, related_name="invoice_lines"
    )

    description = models.CharField(max_length=255)

    #: Signed: an adjustment may be a discount. Visits and fees may not be
    #: negative, which `clean()` enforces -- a negative visit is a mistake
    #: wearing a discount's clothes, and it should be its own labelled line.
    amount_cents = models.IntegerField()

    is_taxable = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta(TenantModel.Meta):
        verbose_name = "Invoice line"
        verbose_name_plural = "Invoice lines"
        ordering = ("position", "created_at")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind=LineKind.ADJUSTMENT) | models.Q(amount_cents__gte=0),
                name="only_an_adjustment_line_may_be_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(kind=LineKind.ADJUSTMENT) | models.Q(job__isnull=False),
                name="every_line_but_an_adjustment_names_a_job",
            ),
        ]
        indexes = [
            *TenantModel.Meta.indexes,
            models.Index(fields=["organization", "job"]),
        ]

    def __str__(self):
        return f"{self.description} ({self.amount_cents}c)"

    def clean(self):
        super().clean()
        errors = {}

        if self.kind != LineKind.ADJUSTMENT:
            if self.amount_cents < 0:
                errors["amount_cents"] = (
                    "Only an adjustment line may be negative. Add a labelled "
                    "adjustment rather than a negative visit."
                )
            if self.job_id is None:
                errors["job"] = "Only an adjustment line may stand without a job."

        if self.invoice_id and self.job_id and self.invoice.customer_id != self.job.customer_id:
            errors["job"] = "That visit belongs to a different customer."

        if errors:
            raise ValidationError(errors)


class Payment(TenantModel):
    """
    Money received against an invoice.

    A Stripe payment (Phase 4b) differs from a check only in that a webhook
    wrote the row and `provider` is set. There is no provider abstraction: one
    provider does not justify an interface, and one designed against a single
    implementation encodes that implementation's shape (ADR-025).
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=16, choices=PaymentMethod.choices)

    #: What settles the invoice. A tip does not, which is why it is separate.
    amount_cents = models.PositiveIntegerField()

    #: Not revenue, not taxed, and never counted toward the balance. Splitting
    #: it among the crew is payroll, which this product does not have yet.
    tip_cents = models.PositiveIntegerField(default=0)

    #: Organization-local date, as written on the check.
    received_on = models.DateField()

    reference = models.CharField(
        max_length=255, blank=True, default="", help_text="Check number, Zelle confirmation, ..."
    )

    #: Null when a provider wrote the row rather than a person.
    recorded_by = models.ForeignKey(
        CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    provider = models.CharField(max_length=32, blank=True, default="")
    provider_reference = models.CharField(max_length=255, blank=True, default="", db_index=True)

    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    void_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta(TenantModel.Meta):
        verbose_name = "Payment"
        verbose_name_plural = "Payments"
        ordering = ("-received_on", "-created_at")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0), name="a_payment_is_for_more_than_nothing"
            ),
            # A provider's own reference is the idempotency key for its
            # webhook: the same event delivered twice must not pay the invoice
            # twice. Soft-deleted rows are included on purpose -- a replay of
            # an event whose row was deleted is still a replay.
            models.UniqueConstraint(
                fields=["provider", "provider_reference"],
                condition=~models.Q(provider=""),
                name="unique_payment_per_provider_reference",
            ),
        ]
        indexes = [
            *TenantModel.Meta.indexes,
            models.Index(fields=["organization", "invoice"]),
            models.Index(fields=["organization", "received_on"]),
        ]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount_cents}c on {self.invoice_id}"

    @property
    def is_void(self) -> bool:
        return self.voided_at is not None

    def delete(self, *args, **kwargs):
        raise ValidationError(
            {
                "detail": (
                    "A payment is voided with a reason, never deleted. Whether the "
                    "customer paid is exactly the dispute this record settles."
                )
            }
        )

    def hard_delete(self, *args, **kwargs):
        raise ValidationError({"detail": "A payment is voided with a reason, never deleted."})


class InvoiceSequence(TenantModel):
    """
    One row per organization: the next invoice number to hand out.

    A separate table rather than `max(number) + 1` because that query cannot be
    locked against a concurrent issue without locking every invoice, and
    because a voided invoice must keep its number spent.
    """

    next_number = models.PositiveIntegerField(default=1)

    class Meta(TenantModel.Meta):
        verbose_name = "Invoice sequence"
        verbose_name_plural = "Invoice sequences"
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(deleted_at__isnull=True),
                name="one_invoice_sequence_per_organization",
            )
        ]

    def __str__(self):
        return f"next {self.next_number}"
