import logging

from django.contrib import admin, messages

from billing import webhook
from billing.models import Invoice, InvoiceLine, InvoiceSequence, Payment, StripeEvent

logger = logging.getLogger(__name__)


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):
    """
    The webhook ledger, read-only. FAILED events are the queue: filter on
    status, read the error, fix the cause, then select the rows and replay
    them with the action below. Nothing here edits a row; the action only
    hands its id back to the worker.
    """

    actions = ("replay_failed",)
    list_display = ("type", "event_id", "account", "organization", "status", "created_at")
    list_filter = ("status", "type", "organization")
    search_fields = ("event_id", "account")
    readonly_fields = (
        "event_id",
        "account",
        "type",
        "organization",
        "status",
        "error",
        "processed_at",
        "payload",
        "created_at",
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_replay_permission(self, request):
        """
        Superusers only. The site has already required a session that cleared
        the second factor (`app/admin.py`); this is on top of that, because a
        replay moves money in a tenant's ledger and viewing the list does not.
        """
        return request.user.is_active and request.user.is_superuser

    @admin.action(description="Re-queue the selected FAILED events", permissions=["replay"])
    def replay_failed(self, request, queryset):
        """
        Hand each selected FAILED event back to `process_stripe_event`.

        This is the replay that used to need a shell on the server. Rows in
        any other status are refused and counted, not silently skipped. Safe
        to run twice: see `billing.webhook.replay`.
        """
        queued, refused, unqueued = [], [], []
        for row in queryset.order_by("created_at"):
            try:
                replayed = webhook.replay(row)
            except Exception:
                # The broker is unreachable. Say so on the page; a bare 500
                # in the middle of an incident tells the operator nothing.
                logger.exception("Could not re-queue Stripe event %s", row.event_id)
                unqueued.append(row)
                continue
            if not replayed:
                refused.append(row)
                continue
            queued.append(row)
            self.log_change(request, row, "Re-queued for processing.")
            logger.info(
                "Stripe event %s (%s) re-queued from the admin by user %s",
                row.event_id,
                row.type,
                request.user.pk,
            )

        if queued:
            self.message_user(
                request,
                f"Re-queued {len(queued)} event(s). Each stays FAILED until a worker has "
                "applied it: reload in a few seconds.",
                messages.SUCCESS,
            )
        if refused:
            statuses = ", ".join(sorted({row.status for row in refused}))
            self.message_user(
                request,
                f"{len(refused)} event(s) not re-queued: only FAILED events that an "
                f"organization owns can be replayed (selected: {statuses}).",
                messages.WARNING,
            )
        if unqueued:
            self.message_user(
                request,
                f"{len(unqueued)} event(s) could not be queued: the broker did not accept "
                "them. Nothing was changed; check Redis and run the action again.",
                messages.ERROR,
            )


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
    # `status` is read-only too, for the reason `PaymentAdmin` is read-only
    # whole: flipping void -> issued here would revive a voided document at
    # its old number, with visits whose claims had already been released.
    # Status moves through `billing.services`, which records why.
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "deleted_at",
        "status",
        "voided_at",
        "voided_by",
        *SNAPSHOT_FIELDS,
    )

    fieldsets = (
        (None, {"fields": ("organization", "customer", "status", "notes")}),  # status read-only
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
