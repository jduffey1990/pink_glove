"""
Send a reminder as soon as a visit is booked or moved into its window (ADR-035).

The hourly task would reach such a visit at the next tick; this closes the
gap between the save and that tick, which matters most for a visit booked in
the evening for the next morning. Both ask `services.due_reminders` the same
question, and the ledger makes whichever answers second a no-op.

Connected in `NotificationsConfig.ready()` with `post_save` on `Job`, so
scheduling never imports this app and every path that saves a single visit
is covered: booking, a PATCH, a reschedule, a plan regenerating its kept
visits, the Django admin. `bulk_create` sends no signal, so the visits a plan
materializes are left to the hourly task, which is soon enough for visits
that were always going to happen.
"""

import datetime as dt

from django.db import transaction
from django.utils import timezone

from notifications.services import remindable_organizations
from notifications.tasks import send_visit_reminder
from scheduling.enums import JobStatus

#: How far ahead a start can be and still be inside the window. The window
#: ends at the close of the organization's local tomorrow, under 33 hours
#: from now in any zone even across a DST change; this is a cheap,
#: generous prefilter so a save far from its visit costs no query and no
#: task. The task decides for real.
LOOKAHEAD = dt.timedelta(hours=48)


def queue_reminder_if_due(sender, instance, raw=False, **kwargs):
    """`post_save` receiver: queue the reminder after the save commits, if it may be due."""
    if raw or instance.status != JobStatus.SCHEDULED or instance.deleted_at is not None:
        return
    now = timezone.now()
    if not now < instance.scheduled_start <= now + LOOKAHEAD:
        return

    if not remindable_organizations().filter(pk=instance.organization_id).exists():
        return

    organization_id, job_id = str(instance.organization_id), str(instance.pk)
    # After the commit, never before: a rolled-back booking must not email
    # anyone, and the worker must be able to read the row it is handed.
    transaction.on_commit(lambda: send_visit_reminder.delay(organization_id, job_id))
