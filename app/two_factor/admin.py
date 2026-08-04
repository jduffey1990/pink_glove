from django.contrib import admin

from two_factor.models import TrustedDevice, TwoFactorCode


@admin.register(TwoFactorCode)
class TwoFactorCodeAdmin(admin.ModelAdmin):
    """Read-only: useful for supporting a locked-out user, never for editing."""

    list_display = ("user", "created_at", "expires_at", "attempts", "consumed_at")
    list_filter = ("consumed_at",)
    search_fields = ("user__email",)
    readonly_fields = (
        "id",
        "user",
        "code_hash",
        "expires_at",
        "consumed_at",
        "attempts",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):
    """Deletable, so support can revoke a lost phone."""

    list_display = ("user", "user_agent", "created_at", "expires_at", "last_used_at")
    search_fields = ("user__email", "user_agent")
    readonly_fields = (
        "id",
        "user",
        "token_hash",
        "expires_at",
        "user_agent",
        "last_used_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
