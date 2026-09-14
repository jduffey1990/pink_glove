"""
The schedule: recurring plans, the jobs they materialize into, and what
happens on a visit.

Two things here are load-bearing and easy to "simplify" wrongly:

* `Job.plan_occurrence` -- the instant an occurrence was *originally* planned
  for, not where it ended up. It is the idempotency key for materialization,
  which is what stops a rescheduled visit being re-created at its old slot the
  next morning. See docs/DECISIONS.md ADR-020.
* `Job.price_cents` -- a snapshot taken at materialization, never read live
  from `Service`. Raising your prices must not retroactively change what last
  month's completed work was worth.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from base.models import TenantModel
from catalog.models import Service
from customers.models import Customer, ServiceLocation
from scheduling.enums import JobStatus
from users.models import CustomUser


class RecurringPlan(TenantModel):
    """
    "Every other Tuesday at 9am, the Hendersons, standard clean."

    Stores the rule; `scheduling.services.materialize_plan` turns it into real
    `Job` rows about eight weeks ahead (ADR-012).
    """

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="plans")
    location = models.ForeignKey(ServiceLocation, on_delete=models.PROTECT, related_name="plans")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="plans")

    rrule = models.TextField(
        help_text=(
            "RFC 5545 RRULE body only, e.g. FREQ=WEEKLY;BYDAY=TU. No DTSTART -- "
            "starts_on and preferred_start_time supply it."
        )
    )
    starts_on = models.DateField(help_text="Local date of the first possible occurrence.")
    ends_on = models.DateField(null=True, blank=True, help_text="Inclusive. Null runs forever.")
    preferred_start_time = models.TimeField(help_text="Local wall-clock time.")

    duration_minutes = models.PositiveIntegerField()

    #: When set, every materialized job snapshots this instead of pricing the
    #: service afresh -- the negotiated rate for this customer's standing slot.
    price_override_cents = models.PositiveIntegerField(null=True, blank=True)

    default_assignees = models.ManyToManyField(
        CustomUser, blank=True, related_name="default_on_plans"
    )

    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True, default="")

    class Meta(TenantModel.Meta):
        verbose_name = "Recurring plan"
        verbose_name_plural = "Recurring plans"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.customer} - {self.service} ({self.rrule})"

    def save(self, *args, **kwargs):
        if not self.duration_minutes and self.service_id:
            self.duration_minutes = self.service.default_duration_minutes
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors = {}

        if self.location_id and self.customer_id and self.location.customer_id != self.customer_id:
            errors["location"] = "That location belongs to a different customer."

        if self.ends_on and self.starts_on and self.ends_on < self.starts_on:
            errors["ends_on"] = "The end date cannot fall before the start date."

        rrule_error = validate_rrule(self.rrule)
        if rrule_error:
            errors["rrule"] = rrule_error

        if errors:
            raise ValidationError(errors)


def validate_rrule(value: str) -> str | None:
    """
    Return an error message for `value`, or None if it is a usable RRULE body.

    DTSTART is rejected outright rather than honoured: the start instant comes
    from `starts_on` + `preferred_start_time` expanded in the organization's
    timezone, and a DTSTART inside the rule would be a second, conflicting
    source of truth for it.

    COUNT is allowed -- it bounds the series, which is a different question
    from where the series begins.

    A UTC-stamped UNTIL (`UNTIL=...Z`) is rejected. Expansion is deliberately
    naive local wall-clock so that "every Tuesday 9am" survives a DST change
    (ADR-012), and dateutil will not mix a naive DTSTART with an aware UNTIL.
    More to the point, "until this UTC instant" is ambiguous against a series
    defined in local time. `ends_on` is the field that means "stop after this
    local date"; a floating `UNTIL=20271231T000000` is accepted as well.
    """
    from dateutil.rrule import rrulestr  # local import: keeps model import cheap

    if not value or not value.strip():
        return "A recurrence rule is required."

    text = value.strip()
    if "DTSTART" in text.upper():
        return (
            "Do not put DTSTART in the rule. The start comes from starts_on and "
            "preferred_start_time, in the organization's timezone."
        )

    if "UNTIL=" in text.upper() and text.upper().split("UNTIL=")[1][:16].endswith("Z"):
        return (
            "Use ends_on rather than a UTC UNTIL. This series is expanded in local "
            "wall-clock time, so a UTC end instant is ambiguous against it. A "
            "floating UNTIL (no trailing Z) is accepted."
        )

    try:
        # A throwaway dtstart: we are checking the rule parses, not expanding it.
        rrulestr(text, dtstart=timezone.now().replace(tzinfo=None))
    except Exception as exc:
        return f"That is not a valid recurrence rule: {exc}"

    return None


class Job(TenantModel):
    """One visit. The unit everything else in the schedule hangs off."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="jobs")
    location = models.ForeignKey(ServiceLocation, on_delete=models.PROTECT, related_name="jobs")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="jobs")

    plan = models.ForeignKey(
        RecurringPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name="jobs"
    )
    plan_occurrence = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "The UTC instant this occurrence was originally planned for. Immutable. "
            "This, not scheduled_start, is the materializer's idempotency key -- a "
            "rescheduled job must not be re-created at its old slot."
        ),
    )

    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()

    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.SCHEDULED, db_index=True
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)

    #: Snapshotted at creation, never read live from `Service`. See module docstring.
    price_cents = models.PositiveIntegerField()

    #: Dispatcher instructions for this visit. What the cleaner reports back
    #: goes in `JobNote` -- keeping them apart means neither overwrites the other.
    notes = models.TextField(blank=True, default="")
    cancellation_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta(TenantModel.Meta):
        verbose_name = "Job"
        verbose_name_plural = "Jobs"
        ordering = ("scheduled_start",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(scheduled_end__gt=models.F("scheduled_start")),
                name="job_ends_after_it_starts",
            ),
            models.UniqueConstraint(
                fields=["plan", "plan_occurrence"],
                condition=models.Q(plan__isnull=False, deleted_at__isnull=True),
                name="unique_job_per_plan_occurrence",
            ),
        ]
        indexes = [
            *TenantModel.Meta.indexes,
            models.Index(fields=["organization", "scheduled_start"]),
            models.Index(fields=["organization", "status", "scheduled_start"]),
        ]

    def __str__(self):
        return f"{self.customer} - {self.service} at {self.scheduled_start:%Y-%m-%d %H:%M}"

    def clean(self):
        super().clean()
        errors = {}

        if self.location_id and self.customer_id and self.location.customer_id != self.customer_id:
            errors["location"] = "That location belongs to a different customer."

        if (
            self.scheduled_start
            and self.scheduled_end
            and self.scheduled_end <= self.scheduled_start
        ):
            errors["scheduled_end"] = "A job must end after it starts."

        if errors:
            raise ValidationError(errors)

    @property
    def is_terminal(self) -> bool:
        from scheduling.enums import TERMINAL_STATUSES

        return self.status in TERMINAL_STATUSES

    @property
    def duration_minutes(self) -> int:
        return int((self.scheduled_end - self.scheduled_start).total_seconds() // 60)

    @property
    def was_rescheduled(self) -> bool:
        """Moved from where the plan originally put it."""
        return self.plan_occurrence is not None and self.scheduled_start != self.plan_occurrence


class JobAssignment(TenantModel):
    """
    Which cleaner is doing this job.

    `user` is a `CustomUser`, which is NOT a `TenantModel` -- so
    `TenantModel._check_tenant_consistency` does not cover it. Membership in
    the job's organization is verified in `scheduling.services.assign_user`,
    which is the only sanctioned way to create one of these.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="assignments")
    user = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="job_assignments")
    assigned_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        verbose_name = "Job assignment"
        verbose_name_plural = "Job assignments"
        ordering = ("assigned_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["job", "user"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_assignment_per_job_user",
            )
        ]

    def __str__(self):
        return f"{self.user} on {self.job_id}"


class TimeEntry(TenantModel):
    """A stretch of time someone was actually on a job."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="time_entries")
    user = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="time_entries")
    clock_in = models.DateTimeField()
    clock_out = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        verbose_name = "Time entry"
        verbose_name_plural = "Time entries"
        ordering = ("-clock_in",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(clock_out__isnull=True)
                | models.Q(clock_out__gt=models.F("clock_in")),
                name="time_entry_ends_after_it_starts",
            ),
            # One open entry per person per job. Closed ones may pile up: a
            # cleaner can legitimately leave and come back.
            models.UniqueConstraint(
                fields=["job", "user"],
                condition=models.Q(clock_out__isnull=True, deleted_at__isnull=True),
                name="one_open_time_entry_per_job_user",
            ),
        ]

    def __str__(self):
        return f"{self.user} on {self.job_id} from {self.clock_in:%Y-%m-%d %H:%M}"

    @property
    def is_open(self) -> bool:
        return self.clock_out is None

    @property
    def duration_minutes(self) -> int | None:
        """None while the entry is still open -- not zero, which would read as worked."""
        if self.clock_out is None:
            return None
        return int((self.clock_out - self.clock_in).total_seconds() // 60)


class JobNote(TenantModel):
    """What the cleaner reports back. Distinct from `Job.notes`, which is
    what the dispatcher sent them in with."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="job_notes")
    user = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="job_notes")
    body = models.TextField()

    class Meta(TenantModel.Meta):
        verbose_name = "Job note"
        verbose_name_plural = "Job notes"
        ordering = ("-created_at",)

    def __str__(self):
        return f"Note on {self.job_id} by {self.user_id}"


class JobPhoto(TenantModel):
    """Before/after evidence. Routinely the thing that settles a dispute."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="photos")
    user = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="job_photos")
    image = models.ImageField(upload_to="job_photos/%Y/%m/")
    caption = models.CharField(max_length=255, blank=True, default="")

    class Meta(TenantModel.Meta):
        verbose_name = "Job photo"
        verbose_name_plural = "Job photos"
        ordering = ("-created_at",)

    def __str__(self):
        return f"Photo on {self.job_id}"
