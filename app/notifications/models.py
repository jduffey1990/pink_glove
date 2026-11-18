"""
What the product has told a customer, and when.

`notifications` depends on `scheduling`, `customers` and `organizations`;
none of them imports it. The ledger points at a visit and the visit never
points back (`related_name="+"`), so the schedule does not know reminders
exist (ADR-035).
"""

from django.db import models

from base.models import TenantModel


class VisitReminder(TenantModel):
    """
    One reminder, about one visit, at one time.

    The row is the claim as well as the record: it is inserted BEFORE the
    email is sent, and the unique constraint below is what makes a second
    beat tick or a retried worker a no-op (ADR-035). A row with `sent_at`
    NULL is a claim whose outcome is unknown -- the worker died between the
    insert and the send -- and it is deliberately never retried, because the
    customer may already have the email. A send that fails with an exception
    removes its row, so the next hourly run tries again.

    A visit is due again whenever its current start differs from the start
    in its latest row (highest `sequence`): a visit moved after its reminder
    is at a time the customer has not been told about, and so is one moved
    back to an earlier time after a second reminder. The latest reminder is
    the one that counts.

    `sequence` numbers a visit's reminders 1, 2, 3... and is unique per
    visit among live rows: two runs that both saw reminder n insert n + 1,
    and only one succeeds. That keeps the claim-before-send lock while
    letting the same start be reminded again after a move away and back.
    """

    job = models.ForeignKey("scheduling.Job", on_delete=models.CASCADE, related_name="+")
    #: The start the customer was told about, copied from the visit when the
    #: reminder was claimed.
    scheduled_start = models.DateTimeField()
    #: Where it went. Copied, because the customer's address can change after.
    recipient = models.EmailField()
    sent_at = models.DateTimeField(null=True, blank=True)
    #: 1 for a visit's first reminder, one more for each after it.
    sequence = models.PositiveIntegerField()

    class Meta(TenantModel.Meta):
        verbose_name = "Visit reminder"
        verbose_name_plural = "Visit reminders"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["job", "sequence"],
                condition=models.Q(deleted_at__isnull=True),
                name="one_reminder_per_visit_sequence",
            ),
        ]

    def __str__(self):
        return f"Reminder for job {self.job_id} at {self.scheduled_start:%Y-%m-%d %H:%M} UTC"
