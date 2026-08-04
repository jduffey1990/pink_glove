from django.contrib import admin

from customers.models import Customer, ServiceLocation


class ServiceLocationInline(admin.TabularInline):
    model = ServiceLocation
    extra = 0
    fields = ("label", "line1", "city", "postal_code", "square_feet", "is_active")
    show_change_link = True


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("display_name", "organization", "email", "phone", "status", "created_at")
    list_filter = ("organization", "status", "preferred_contact_method")
    search_fields = ("first_name", "last_name", "company_name", "email", "phone")
    autocomplete_fields = ("organization", "user")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")
    inlines = (ServiceLocationInline,)


@admin.register(ServiceLocation)
class ServiceLocationAdmin(admin.ModelAdmin):
    list_display = ("label", "one_line_address", "customer", "square_feet", "is_active")
    list_filter = ("organization", "is_active", "has_pets", "state")
    # gate_code / alarm_code / key_location are encrypted and cannot be searched.
    search_fields = ("label", "line1", "city", "postal_code", "customer__last_name")
    autocomplete_fields = ("organization", "customer")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")

    fieldsets = (
        (None, {"fields": ("organization", "customer", "label", "is_active")}),
        (
            "Address",
            {"fields": ("line1", "line2", "city", "state", "postal_code", "country")},
        ),
        ("Size", {"fields": ("square_feet", "bedrooms", "bathrooms")}),
        (
            "Access",
            {
                "fields": (
                    "parking_notes",
                    "access_notes",
                    "gate_code",
                    "alarm_code",
                    "key_location",
                ),
                "description": "Gate code, alarm code, and key location are encrypted at rest.",
            },
        ),
        ("Pets", {"fields": ("has_pets", "pet_notes")}),
        ("Audit", {"fields": ("id", "created_at", "updated_at", "deleted_at")}),
    )
