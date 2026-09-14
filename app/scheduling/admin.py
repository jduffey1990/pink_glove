"""
Admin for the schedule.

`raw_id_fields` rather than `autocomplete_fields` on Job and RecurringPlan:
each carries four foreign keys, and the autocomplete widgets turn a single
job page into a handful of extra queries for lists an admin rarely browses.
"""

from django.contrib import admin

from scheduling.models import (
    Job,
    JobAssignment,
    JobNote,
    JobPhoto,
    RecurringPlan,
    TimeEntry,
)


class JobAssignmentInline(admin.TabularInline):
    model = JobAssignment
    extra = 0
    raw_id_fields = ("organization", "user", "assigned_by")
    fields = ("user", "assigned_at", "accepted_at")
    readonly_fields = ("assigned_at",)


class TimeEntryInline(admin.TabularInline):
    model = TimeEntry
    extra = 0
    raw_id_fields = ("organization", "user")
    fields = ("user", "clock_in", "clock_out")


@admin.register(RecurringPlan)
class RecurringPlanAdmin(admin.ModelAdmin):
    list_display = (
        "customer",
        "service",
        "rrule",
        "preferred_start_time",
        "starts_on",
        "ends_on",
        "is_active",
    )
    list_filter = ("organization", "is_active", "service")
    search_fields = ("customer__last_name", "customer__company_name", "location__line1")
    raw_id_fields = ("organization", "customer", "location", "service")
    filter_horizontal = ("default_assignees",)
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        "scheduled_start",
        "customer",
        "service",
        "status",
        "price_cents",
        "organization",
    )
    list_filter = ("organization", "status", "service")
    date_hierarchy = "scheduled_start"
    search_fields = ("customer__last_name", "customer__company_name", "location__line1")
    raw_id_fields = ("organization", "customer", "location", "service", "plan")
    # plan_occurrence is the materializer's idempotency key. Editing it by hand
    # would let the next daily run re-create a duplicate of this visit.
    readonly_fields = (
        "id",
        "plan_occurrence",
        "status_changed_at",
        "created_at",
        "updated_at",
        "deleted_at",
    )
    inlines = (JobAssignmentInline, TimeEntryInline)


@admin.register(JobAssignment)
class JobAssignmentAdmin(admin.ModelAdmin):
    list_display = ("job", "user", "assigned_at", "accepted_at")
    list_filter = ("organization",)
    raw_id_fields = ("organization", "job", "user", "assigned_by")
    readonly_fields = ("id", "assigned_at", "created_at", "updated_at", "deleted_at")


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display = ("job", "user", "clock_in", "clock_out", "duration_minutes")
    list_filter = ("organization",)
    date_hierarchy = "clock_in"
    raw_id_fields = ("organization", "job", "user")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")


@admin.register(JobNote)
class JobNoteAdmin(admin.ModelAdmin):
    list_display = ("job", "user", "created_at")
    list_filter = ("organization",)
    search_fields = ("body",)
    raw_id_fields = ("organization", "job", "user")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")


@admin.register(JobPhoto)
class JobPhotoAdmin(admin.ModelAdmin):
    list_display = ("job", "user", "caption", "created_at")
    list_filter = ("organization",)
    raw_id_fields = ("organization", "job", "user")
    readonly_fields = ("id", "created_at", "updated_at", "deleted_at")
