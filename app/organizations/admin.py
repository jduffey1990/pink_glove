from django.contrib import admin

from organizations.models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active", "created_at")
    list_filter = ("is_active", "timezone")
    search_fields = ("name", "slug", "email", "phone")
    readonly_fields = ("id", "slug", "created_at", "updated_at", "deleted_at")

    fieldsets = (
        (None, {"fields": ("name", "slug", "timezone", "is_active")}),
        (
            "Contact",
            {
                "fields": (
                    "email",
                    "phone",
                    "address_line1",
                    "address_line2",
                    "address_city",
                    "address_state",
                    "address_postal_code",
                    "address_country",
                )
            },
        ),
        ("Branding", {"fields": ("logo", "primary_color")}),
        (
            "Billing (Phase 4)",
            {
                "fields": ("stripe_customer_id", "stripe_subscription_id"),
                "classes": ("collapse",),
            },
        ),
        ("Audit", {"fields": ("id", "created_at", "updated_at", "deleted_at")}),
    )
