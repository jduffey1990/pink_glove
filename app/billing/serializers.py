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
from billing import services
from billing.enums import InvoiceStatus, LineKind, PaymentMethod, PaymentState
from billing.models import Invoice, InvoiceLine, Payment
from scheduling.serializers import CustomerSummarySerializer


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

    def validate_invoice(self, value):
        if value.status != InvoiceStatus.DRAFT:
            raise serializers.ValidationError(
                "That invoice is issued. Corrections after issue are a void and a new invoice."
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
            "is_void",
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
            "is_void",
            "voided_at",
            "void_reason",
            "created_at",
        )
        extra_kwargs = {"tip_cents": {"required": False}}


class InvoiceSerializer(TenantModelSerializer):
    """
    The dispatcher's view of an invoice.

    `subtotal_cents`, `tax_cents` and `total_cents` are the *stored* snapshot
    on an issued invoice and a live calculation on a draft, which is what
    `services.totals` decides -- so a draft shows what issuing it would come
    to, and an issued invoice shows what the customer was sent.
    """

    customer_detail = CustomerSummarySerializer(source="customer", read_only=True)
    lines = InvoiceLineSerializer(many=True, read_only=True)
    payments = serializers.SerializerMethodField()

    subtotal_cents = serializers.SerializerMethodField()
    tax_cents = serializers.SerializerMethodField()
    total_cents = serializers.SerializerMethodField()

    payment_state = serializers.SerializerMethodField()
    balance_cents = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()

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
            "is_overdue",
            "available_actions",
            "sent_at",
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

    def get_is_overdue(self, obj) -> bool:
        return services.is_overdue(obj)

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
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
