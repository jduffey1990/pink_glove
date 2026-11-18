"""
Visit reminders: which visits are due one, and sending it (ADR-035).

A reminder goes out on the organization-local day before the visit, from
`REMINDER_LOCAL_HOUR` onward. The task runs hourly, so "from the hour on"
rather than "at the hour" is what lets a missed tick, a worker that was down
or a visit booked in the afternoon still be reminded the same day. What
stops the hourly runs from repeating one is the `VisitReminder` ledger, not
the clock.

Reckoned in `Organization.timezone` and compared in UTC (invariant 8):
"tomorrow" is the organization's tomorrow, and `local_day_bounds` turns it
into a UTC interval that is 23 or 25 hours long across a DST change.
"""

import datetime as dt
import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, QuerySet
from django.template.loader import render_to_string

from customers.enums import CustomerStatus
from notifications.models import VisitReminder
from organizations.models import Organization
from scheduling.enums import JobStatus
from scheduling.models import Job

logger = logging.getLogger(__name__)

#: The organization-local hour on the day before a visit from which its
#: reminder may go. Inside the default business hours, so a customer who
#: replies or rings about the visit reaches somebody the same day.
REMINDER_LOCAL_HOUR = 9

#: A lead with a visit booked is about to be a customer; an inactive or
#: archived one has been told nothing about a visit that somebody forgot to
#: cancel, and an email is the wrong way for them to find out.
REMINDABLE_CUSTOMER_STATUSES = (CustomerStatus.LEAD, CustomerStatus.ACTIVE)


def reminder_window(
    organization: Organization, now: dt.datetime
) -> tuple[dt.datetime, dt.datetime] | None:
    """
    The UTC interval whose visits are due a reminder at `now`, or None
    before the reminder hour. Half-open, like `local_day_bounds`.
    """
    local_now = now.astimezone(organization.tz)
    if local_now.hour < REMINDER_LOCAL_HOUR:
        return None
    tomorrow = local_now.date() + dt.timedelta(days=1)
    return organization.local_day_bounds(tomorrow, tomorrow)


def due_reminders(organization: Organization, now: dt.datetime) -> QuerySet[Job]:
    """
    The visits in `organization` that should be reminded at `now` and have
    not been, at their current time.

    Every condition is here and nowhere else, so the tests can walk each
    one. The joins name `deleted_at` themselves: the default manager hides a
    soft-deleted job, not a soft-deleted customer or location behind it.
    """
    if not (organization.is_active and organization.visit_reminders_enabled):
        return Job.objects.none()

    window = reminder_window(organization, now)
    if window is None:
        return Job.objects.none()
    start, end = window

    already_sent = VisitReminder.objects.filter(
        job=OuterRef("pk"), scheduled_start=OuterRef("scheduled_start")
    )

    return (
        Job.objects.filter(
            organization=organization,
            # Only a visit still waiting to happen. A terminal one is over or
            # called off; one en route or in progress is already under way.
            status=JobStatus.SCHEDULED,
            scheduled_start__gte=start,
            scheduled_start__lt=end,
            customer__deleted_at__isnull=True,
            customer__status__in=REMINDABLE_CUSTOMER_STATUSES,
            location__deleted_at__isnull=True,
        )
        .exclude(customer__email="")
        .exclude(Exists(already_sent))
        .select_related("organization", "customer", "location", "service")
        .order_by("scheduled_start")
    )


def _clock(instant: dt.datetime) -> str:
    """9:00 AM, without the platform-specific `%-I`."""
    return f"{instant.hour % 12 or 12}:{instant:%M} {'AM' if instant.hour < 12 else 'PM'}"


def reminder_email_context(job: Job) -> dict:
    """
    Everything the reminder prints, already formatted in the organization's
    timezone. Formatted here, not with the template's `date` filter, which
    would convert to the server's zone (UTC).

    No access notes and no codes: an email is not the audited reveal
    (ADR-014), and the customer wrote them.
    """
    organization = job.organization
    start = job.scheduled_start.astimezone(organization.tz)
    end = job.scheduled_end.astimezone(organization.tz)
    return {
        "organization": organization,
        "customer_name": job.customer.first_name or job.customer.display_name,
        "service_name": job.service.name,
        "day": f"{start:%A} {start.day} {start:%B}",
        "start_time": _clock(start),
        "end_time": _clock(end),
        "address": job.location.one_line_address,
    }


def _claim(job: Job) -> VisitReminder | None:
    """
    Record the reminder before sending it. None if another run already has.

    The insert is the lock: two runs racing for one visit both reach here,
    and Postgres lets one insert through and makes the other wait, then
    raise on the unique constraint.
    """
    try:
        with transaction.atomic():
            return VisitReminder.objects.create(
                organization_id=job.organization_id,
                job=job,
                scheduled_start=job.scheduled_start,
                recipient=job.customer.email,
            )
    except IntegrityError:
        return None


def send_reminder(job: Job, now: dt.datetime) -> bool:
    """
    Claim, send, stamp. True only if this call sent the email.

    Sent through Django's configured mail backend like every other email
    here: console under local settings, locmem under test settings, and only
    a deployed environment's own SMTP configuration reaches anybody.
    """
    reminder = _claim(job)
    if reminder is None:
        return False

    context = reminder_email_context(job)
    organization = job.organization
    subject = f"Reminder: {organization.name} is coming {context['day']} at {context['start_time']}"
    body = render_to_string("notifications/visit_reminder_email.html", context)

    try:
        message = EmailMessage(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [reminder.recipient],
            # A customer who answers is answering the cleaning company.
            reply_to=[organization.email] if organization.email else None,
        )
        message.content_subtype = "html"
        message.send(fail_silently=False)
    except Exception:
        # No address in the log line: the job id is enough to find it.
        logger.exception("Failed to send the visit reminder for job %s", job.pk)
        # Free the claim so the next hourly run tries again.
        reminder.hard_delete()
        return False

    reminder.sent_at = now
    reminder.save(update_fields=["sent_at", "updated_at"])
    return True


def send_due_reminders(organization: Organization, now: dt.datetime) -> int:
    """Send every reminder due in one organization at `now`. Returns how many went."""
    return sum(send_reminder(job, now) for job in due_reminders(organization, now))
