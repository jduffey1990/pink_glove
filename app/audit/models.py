"""
Audit trail for access to sensitive client information.

Deliberately narrow. Ordinary schedule data -- address, phone, start time, what
the job involves -- is not logged; a worker needs all of it constantly and
recording every glance would bury the signal. What is logged is the explicit
act of revealing a home's access codes, which is a discrete, intentional
click that maps to a real-world moment: someone is at a door.

Flagging is NOT decided here. `is_flagged` is set by an end-of-day pass
(Phase 3), because whether a reveal fell inside its appointment window depends
on the schedule as it finally stood that day -- a job moved at 4pm changes the
verdict on a 2pm reveal. A flag is a prompt for a human to ask a question, not
an accusation and not an enforcement action.
"""

from django.db import models

from base.models import TenantModel
from customers.models import ServiceLocation
from users.models import CustomUser

#: Shown before a reveal. The user must acknowledge it, and that
#: acknowledgement is recorded. Kept here so the API and the frontend cannot
#: drift into showing different warnings.
ACCESS_WARNING = (
    "All access to sensitive client information is logged. If this is accessed "
    "outside of regular operating use, it will be flagged to management. Continue?"
)


class AccessReveal(TenantModel):
    """
    One reveal of one location's access codes. Append-only.

    Rows are never edited or deleted -- an audit log its own subject can amend
    is not an audit log. `delete()` raises; the admin registers it read-only.
    """

    user = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="access_reveals")
    location = models.ForeignKey(
        ServiceLocation, on_delete=models.PROTECT, related_name="access_reveals"
    )
    # Phase 3 adds: job = FK("scheduling.Job", null=True) -- binds a reveal to
    # the specific visit it was for, which is what the EOD pass compares against.

    fields_revealed = models.JSONField(default=list, help_text='e.g. ["gate_code", "alarm_code"]')
    acknowledged = models.BooleanField(
        default=False, help_text="User confirmed the logging warning before proceeding."
    )

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")

    # --- Set by the end-of-day pass (Phase 3), not at reveal time ------------
    evaluated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_flagged = models.BooleanField(default=False, db_index=True)
    flag_reason = models.CharField(max_length=255, blank=True, default="")

    # --- Human review of a flag ---------------------------------------------
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="access_reviews",
    )
    review_note = models.TextField(blank=True, default="")

    class Meta(TenantModel.Meta):
        verbose_name = "Access reveal"
        verbose_name_plural = "Access reveals"
        ordering = ("-created_at",)
        indexes = [
            *TenantModel.Meta.indexes,
            models.Index(fields=["organization", "user", "created_at"]),
            models.Index(fields=["organization", "is_flagged", "created_at"]),
        ]

    def __str__(self):
        return f"{self.user_id} revealed {self.location_id} at {self.created_at:%Y-%m-%d %H:%M}"

    def delete(self, *args, **kwargs):
        raise NotImplementedError(
            "AccessReveal rows are append-only. If retention requires purging old "
            "entries, add an explicit, documented retention command."
        )

    def hard_delete(self, *args, **kwargs):
        raise NotImplementedError("AccessReveal rows are append-only.")

    @property
    def is_reviewed(self) -> bool:
        return self.reviewed_at is not None
