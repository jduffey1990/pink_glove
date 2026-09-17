from django.contrib import admin

from catalog.models import Service


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "organization",
        "pricing_model",
        "base_price_cents",
        "default_duration_minutes",
        "is_active",
    )
    list_filter = ("organization", "pricing_model", "is_active", "is_taxable")
    search_fields = ("name", "description")
    autocomplete_fields = ("organization",)
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")

    fieldsets = (
        (None, {"fields": ("organization", "name", "description", "is_active")}),
        (
            "Pricing",
            {
                "fields": (
                    "pricing_model",
                    "base_price_cents",
                    "hourly_rate_cents",
                    "per_sqft_rate_cents",
                    "is_taxable",
                ),
                "description": (
                    "All amounts in cents. base_price_cents doubles as the minimum "
                    "charge for hourly and per-square-foot services."
                ),
            },
        ),
        ("Scheduling", {"fields": ("default_duration_minutes",)}),
        ("Audit", {"fields": ("id", "created_at", "updated_at", "deleted_at")}),
    )
