"""
Serializers for billing.

Two habits from earlier phases carry over. The invoice publishes what the
caller may *do* -- `available_actions`, `payment_state`, `balance_cents`,
`is_overdue` -- so a page draws its buttons from the server's rules instead of
keeping a copy (ADR-023, the same shape as `Job.next_statuses`). And every
amount the customer will read is a snapshot on the row, not a live
recalculation (ADR-026).
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from base.viewsets import TenantModelSerializer
from billing import connect, services
from billing.enums import LineKind, PaymentMethod, PaymentState
from billing.models import Invoice, InvoiceLine, Payment
from customers.serializers import CustomerSummarySerializer


class InvoiceLineSerializer(TenantModelSerializer):
    """
    A line. Only an adjustment is writable from scratch: visit and fee lines
    are built by `draft_invoice` from the visit itself, and a hand-typed
    "visit" line would be a number with no job behind it.
    """

    class Meta(TenantModelSerializer.Meta):
        model = InvoiceLine
        fields = (
            "id",
            "organization",
            "invoice",
            "kind",
            "job",
            "description",
            "amount_cents",
            "is_taxable",
            "position",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "organization", "job", "created_at", "updated_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Scope the invoice field to the caller's own organization.
        #
        # `TenantModel.save()` would refuse a cross-tenant write anyway, but
        # only after `validate_invoice` below has already answered "that
        # invoice is issued" -- which confirms both that another tenant's
        # invoice exists and what state it is in. Narrowing the queryset makes
        # the answer "does not exist" for an id from anywhere else, which is
        # the same answer this codebase gives a cross-tenant read everywhere
        # else (a 403 would confirm the record exists; so does a 400 that
        # describes it).
        request = self.context.get("request")
        organization = getattr(request, "organization", None)
        field = self.fields.get("invoice")

        if field is not None and hasattr(field, "queryset"):
            field.queryset = (
                Invoice.objects.filter(organization=organization)
                if organization is not None
                else Invoice.objects.none()
            )

    def validate_kind(self, value):
        if value != LineKind.ADJUSTMENT:
            raise serializers.ValidationError(
                "Only an adjustment can be written by hand. Visit and no-access "
                "lines come from the visits themselves."
            )
        return value

    def validate(self, attrs):
        # `kind` is not in a PATCH body, so read it off the instance too.
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        amount = attrs.get("amount_cents", getattr(self.instance, "amount_cents", None))

        if kind != LineKind.ADJUSTMENT and amount is not None and amount < 0:
            raise serializers.ValidationError(
                {"amount_cents": "Only an adjustment line may be negative."}
            )
        return attrs


class PaymentSerializer(TenantModelSerializer):
    """
    A payment. Written once and thereafter only voided (ADR-025), so every
    field but the ones `record_payment` takes is read-only.
    """

    recorded_by_name = serializers.CharField(source="recorded_by.full_name", read_only=True)
    is_void = serializers.BooleanField(read_only=True)
    #: Server-owned: a card payment is reversed by a refund in Stripe, which
    #: arrives as an event, so the void button is never offered for one.
    can_void = serializers.SerializerMethodField()

    class Meta(TenantModelSerializer.Meta):
        model = Payment
        fields = (
            "id",
            "organization",
            "invoice",
            "method",
            "amount_cents",
            "tip_cents",
            "received_on",
            "reference",
            "recorded_by",
            "recorded_by_name",
            "provider",
            "provider_reference",
            "fee_cents",
            "disputed_at",
            "dispute_status",
            "is_void",
            "can_void",
            "voided_at",
            "void_reason",
            "created_at",
        )
        read_only_fields = (
            "id",
            "organization",
            "recorded_by",
            "recorded_by_name",
            "provider",
            "provider_reference",
            "fee_cents",
            "disputed_at",
            "dispute_status",
            "is_void",
            "can_void",
            "voided_at",
            "void_reason",
            "created_at",
        )
        extra_kwargs = {"tip_cents": {"required": False}}

    def get_can_void(self, obj) -> bool:
        return services.can_void_by_hand(obj)


class InvoiceSerializer(TenantModelSerializer):
    """
    The dispatcher's view of an invoice.

    `subtotal_cents`, `tax_cents` and `total_cents` are the *stored* snapshot
    on an issued invoice and a live calculation on a draft, which is what
    `services.totals` decides -- so a draft shows what issuing it would come
    to, and an issued invoice shows what the customer was sent.
    """

    customer_detail = CustomerSummarySerializer(source="customer", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True)
    issued_by_name = serializers.CharField(source="issued_by.full_name", read_only=True)
    lines = InvoiceLineSerializer(many=True, read_only=True)
    payments = serializers.SerializerMethodField()

    subtotal_cents = serializers.SerializerMethodField()
    tax_cents = serializers.SerializerMethodField()
    total_cents = serializers.SerializerMethodField()

    payment_state = serializers.SerializerMethodField()
    balance_cents = serializers.SerializerMethodField()
    overpaid_cents = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()
    #: The customer's pay-by-card link, when there is one to give (Phase 4b):
    #: Stripe enabled, the organization taking cards, the invoice open with a
    #: balance. Null otherwise -- the server decides, not the page.
    pay_url = serializers.SerializerMethodField()

    class Meta(TenantModelSerializer.Meta):
        model = Invoice
        fields = (
            "id",
            "organization",
            "customer",
            "customer_detail",
            "number",
            "status",
            "issued_on",
            "due_on",
            "bill_to_name",
            "bill_to_email",
            "bill_to_address",
            "tax_rate_percent",
            "subtotal_cents",
            "tax_cents",
            "total_cents",
            "notes",
            "lines",
            "payments",
            "payment_state",
            "balance_cents",
            "overpaid_cents",
            "is_overdue",
            "available_actions",
            "pay_url",
            "sent_at",
            "created_by_name",
            "issued_by_name",
            "voided_at",
            "void_reason",
            "created_at",
            "updated_at",
        )
        # Only `notes` is editable, and only while the invoice is a draft --
        # the viewset enforces that. Everything else is either the snapshot or
        # derived from the ledger.
        read_only_fields = tuple(field for field in fields if field not in ("notes",))

    def _totals(self, obj) -> dict[str, int]:
        if not hasattr(obj, "_cached_totals"):
            obj._cached_totals = (
                services.totals(obj)
                if obj.is_draft
                else {
                    "subtotal_cents": obj.subtotal_cents,
                    "tax_cents": obj.tax_cents,
                    "total_cents": obj.total_cents,
                }
            )
        return obj._cached_totals

    def get_subtotal_cents(self, obj) -> int:
        return self._totals(obj)["subtotal_cents"]

    def get_tax_cents(self, obj) -> int:
        return self._totals(obj)["tax_cents"]

    def get_total_cents(self, obj) -> int:
        return self._totals(obj)["total_cents"]

    @extend_schema_field(serializers.ChoiceField(choices=PaymentState.choices))
    def get_payment_state(self, obj) -> str:
        return services.payment_state(obj)

    def get_balance_cents(self, obj) -> int:
        return services.balance_cents(obj)

    def get_overpaid_cents(self, obj) -> int:
        return services.overpaid_cents(obj)

    def get_is_overdue(self, obj) -> bool:
        return services.is_overdue(obj)

    def get_pay_url(self, obj) -> str | None:
        return connect.pay_url_if_payable(obj)

    @extend_schema_field(
        serializers.ListField(
            child=serializers.ChoiceField(
                choices=[(action, action) for action in services.INVOICE_ACTIONS]
            )
        )
    )
    def get_available_actions(self, obj) -> list[str]:
        return services.available_actions(obj)

    @extend_schema_field(PaymentSerializer(many=True))
    def get_payments(self, obj):
        return PaymentSerializer(obj.payments.all(), many=True, context=self.context).data


# --- Action and response bodies --------------------------------------------


class DraftInvoiceSerializer(serializers.Serializer):
    """`POST /api/billing/invoices/` -- a customer and the visits to bill."""

    customer = serializers.UUIDField()
    jobs = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class RecordPaymentSerializer(serializers.Serializer):
    """`POST /api/billing/payments/` -- everything a person types on a receipt."""

    invoice = serializers.UUIDField()
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    amount_cents = serializers.IntegerField(min_value=1)
    tip_cents = serializers.IntegerField(min_value=0, required=False, default=0)
    received_on = serializers.DateField()
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class UrlSerializer(serializers.Serializer):
    """Somewhere to send the browser: Stripe onboarding, or Checkout."""

    url = serializers.URLField()


class PublicInvoiceLineSerializer(serializers.Serializer):
    description = serializers.CharField()
    amount_cents = serializers.IntegerField()


class PublicInvoiceSerializer(serializers.Serializer):
    """
    The invoice as its customer sees it on the pay page (Phase 4b).

    Reached by a signed token, not a session, so it says what the invoice
    email said and nothing more: no customer record, no address book, no
    ids of anything else. `payable_reason` is the server's own sentence for
    why the button is absent (ADR-023).
    """

    organization_name = serializers.CharField()
    number = serializers.CharField()
    status = serializers.CharField()
    issued_on = serializers.DateField(allow_null=True)
    due_on = serializers.DateField(allow_null=True)
    bill_to_name = serializers.CharField(allow_blank=True)
    lines = PublicInvoiceLineSerializer(many=True)
    subtotal_cents = serializers.IntegerField()
    tax_rate_percent = serializers.DecimalField(max_digits=6, decimal_places=3)
    tax_cents = serializers.IntegerField()
    total_cents = serializers.IntegerField()
    paid_cents = serializers.IntegerField()
    balance_cents = serializers.IntegerField()
    payment_state = serializers.ChoiceField(choices=PaymentState.choices)
    notes = serializers.CharField(allow_blank=True)
    footer = serializers.CharField(allow_blank=True)
    payable_reason = serializers.CharField(allow_null=True)

    @classmethod
    def from_invoice(cls, invoice: Invoice) -> "PublicInvoiceSerializer":
        organization = invoice.organization
        return cls(
            {
                "organization_name": organization.name,
                "number": invoice.number,
                "status": invoice.status,
                "issued_on": invoice.issued_on,
                "due_on": invoice.due_on,
                "bill_to_name": invoice.bill_to_name,
                "lines": [
                    {"description": line.description, "amount_cents": line.amount_cents}
                    for line in invoice.lines.all()
                ],
                "subtotal_cents": invoice.subtotal_cents,
                "tax_rate_percent": invoice.tax_rate_percent,
                "tax_cents": invoice.tax_cents,
                "total_cents": invoice.total_cents,
                "paid_cents": services.paid_cents(invoice),
                "balance_cents": services.balance_cents(invoice),
                "payment_state": services.payment_state(invoice),
                "notes": invoice.notes,
                "footer": organization.invoice_footer,
                "payable_reason": connect.payable(invoice),
            }
        )


class BillableJobSerializer(serializers.Serializer):
    """One row of the ready-to-invoice list, priced as it will bill."""

    id = serializers.UUIDField()
    customer = serializers.UUIDField()
    customer_name = serializers.CharField()
    service_name = serializers.CharField()
    description = serializers.CharField()
    scheduled_start = serializers.DateTimeField()
    status = serializers.CharField()
    amount_cents = serializers.IntegerField()
