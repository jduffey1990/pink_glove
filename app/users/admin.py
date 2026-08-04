from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from users.models import CustomUser, Membership


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    fields = ("organization", "role", "is_active", "invited_at", "accepted_at")
    autocomplete_fields = ("organization",)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "is_active", "created_at")
    list_filter = ("role", "is_active", "organization")
    search_fields = ("user__email", "organization__name")
    autocomplete_fields = ("user", "organization")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "is_active", "is_staff", "created_at")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "first_name", "last_name", "phone")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at", "last_login")
    inlines = (MembershipInline,)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal", {"fields": ("first_name", "last_name", "phone")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Audit", {"fields": ("id", "created_at", "updated_at", "deleted_at", "last_login")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "first_name", "last_name"),
            },
        ),
    )
