from django.contrib import admin

from organizations.models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active", "billing_exempt", "created_at")
    list_filter = ("is_active", "billing_exempt", "timezone")
    search_fields = ("name", "slug", "email", "phone")
    readonly_fields = (
        "id",
        "slug",
        "created_at",
        "updated_at",
        "deleted_at",
        # Mirrors of Stripe (ADR-028): re-read from it, never typed in here.
        "stripe_customer_id",
        "stripe_subscription_id",
        "subscription_status",
        "subscription_paid_through",
        "subscription_cancel_at_period_end",
    )

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
            "Invoicing",
            {
                "fields": (
                    "tax_rate_percent",
                    "no_access_fee_type",
                    "no_access_fee_value",
                    "invoice_prefix",
                    "invoice_terms_days",
                    "invoice_footer",
                ),
                "description": (
                    "Frozen onto each invoice when it is issued, so editing one of "
                    "these never changes a document that has already gone out. The "
                    "no-access fee value is cents when the type is flat and a "
                    "percentage when it is proportional."
                ),
            },
        ),
        (
            "Platform subscription (Phase 4c)",
            {
                "fields": (
                    "billing_exempt",
                    "trial_ends_at",
                    "subscription_status",
                    "subscription_paid_through",
                    "subscription_cancel_at_period_end",
                    "deactivated_for_billing_at",
                    "stripe_customer_id",
                    "stripe_subscription_id",
                ),
                "description": (
                    "Exempt means comped: never deactivated for billing. Move the trial end "
                    "to give a pilot longer. The rest is read from Stripe by the daily task "
                    "and the webhook. An organization deactivated for billing has the stamp "
                    "set and comes back on its own when a subscription is paid; one switched "
                    "off here, by hand, stays off whatever Stripe says. To keep a lapsed "
                    "organization off even if it pays, clear the stamp; to switch one back on "
                    "while it is still uncovered, clear the stamp too, or the daily task will "
                    "switch it off again."
                ),
            },
        ),
        ("Audit", {"fields": ("id", "created_at", "updated_at", "deleted_at")}),
    )
