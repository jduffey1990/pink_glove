from django.contrib import admin

from audit.models import AccessReveal


@admin.register(AccessReveal)
class AccessRevealAdmin(admin.ModelAdmin):
    """
    Fully read-only. An audit log that its own subjects can edit or delete is
    not an audit log, and the admin is exactly where someone would try.
    """

    list_display = (
        "created_at",
        "user",
        "location",
        "fields_revealed",
        "is_flagged",
        "is_reviewed",
    )
    list_filter = ("organization", "is_flagged", "acknowledged", "created_at")
    search_fields = ("user__email", "location__label", "location__line1")
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(boolean=True, description="Reviewed")
    def is_reviewed(self, obj):
        return obj.is_reviewed
