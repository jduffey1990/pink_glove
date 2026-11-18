from django.contrib import admin

from notifications.models import VisitReminder


@admin.register(VisitReminder)
class VisitReminderAdmin(admin.ModelAdmin):
    """
    Read-only: the ledger is written by the reminder task alone (ADR-035).
    Deleting a row here would let the task send that reminder again.
    """

    list_display = (
        "job",
        "organization",
        "sequence",
        "scheduled_start",
        "recipient",
        "sent_at",
        "created_at",
    )
    list_filter = ("organization",)
    list_select_related = ("job__customer", "job__service", "organization")
    search_fields = ("recipient",)
    readonly_fields = (
        "id",
        "organization",
        "job",
        "sequence",
        "scheduled_start",
        "recipient",
        "sent_at",
        "created_at",
        "updated_at",
        "deleted_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
