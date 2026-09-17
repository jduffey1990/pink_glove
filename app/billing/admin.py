from django.contrib import admin

from billing.models import Invoice, InvoiceLine, InvoiceSequence, Payment

SNAPSHOT_FIELDS = (
    "number",
    "issued_on",
    "due_on",
    "bill_to_name",
    "bill_to_email",
    "bill_to_address",
    "tax_rate_percent",
    "subtotal_cents",
    "tax_cents",
    "total_cents",
    "sent_at",
)


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    fields = ("position", "kind", "description", "amount_cents", "is_taxable", "job")
    readonly_fields = ("job",)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("__str__", "organization", "customer", "status", "total_cents", "due_on")
    list_filter = ("organization", "status")
    search_fields = ("number", "bill_to_name", "bill_to_email")
    autocomplete_fields = ("organization", "customer")
    inlines = [InvoiceLineInline]
    # The snapshot is written once, at issue (ADR-026). Editing it here would
    # change a document the customer has already read.
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "deleted_at",
        "voided_at",
        "voided_by",
        *SNAPSHOT_FIELDS,
    )

    fieldsets = (
        (None, {"fields": ("organization", "customer", "status", "notes")}),
        (
            "Snapshot",
            {
                "fields": SNAPSHOT_FIELDS,
                "description": (
                    "Written when the invoice was issued and never recomputed. "
                    "Corrections are a void and a new invoice."
                ),
            },
        ),
        ("Void", {"fields": ("voided_at", "voided_by", "void_reason")}),
        ("Audit", {"fields": ("id", "created_at", "updated_at", "deleted_at")}),
    )


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """
    Read-only. A payment is voided through the API, which records who and why;
    an admin that could amend one would defeat the point of the ledger.
    """

    list_display = (
        "invoice",
        "method",
        "amount_cents",
        "tip_cents",
        "received_on",
        "voided_at",
    )
    list_filter = ("organization", "method", "provider")
    search_fields = ("reference", "provider_reference")
    readonly_fields = [field.name for field in Payment._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InvoiceSequence)
class InvoiceSequenceAdmin(admin.ModelAdmin):
    list_display = ("organization", "next_number")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")
