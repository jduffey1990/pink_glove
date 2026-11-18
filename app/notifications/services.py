"""
Visit reminders: which visits are due one, and sending it (ADR-035).

A visit is due its reminder once it is `REMINDER_LOCAL_HOUR` on the
organization-local day before it, until it starts. The hourly task asks
that question every hour, so a missed tick, a worker that was down, or a
visit booked at 23:30 for the next morning is still reminded before it
begins (at the next tick). What stops a reminder being
repeated is the `VisitReminder` ledger, not the clock.

Reckoned in `Organization.timezone` and compared in UTC (invariant 8):
"the day before" is the organization's, and `local_day_bounds` turns the
last due day into a UTC boundary that is right across a DST change.
"""

import datetime as dt
import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.db.models import F, OuterRef, Q, QuerySet, Subquery
from django.template.loader import render_to_string

from customers.enums import CustomerStatus
from notifications.models import VisitReminder
from organizations.models import Organization
from scheduling.enums import JobStatus
from scheduling.models import Job

logger = logging.getLogger(__name__)

#: The organization-local hour on the day before a visit from which its
#: reminder may go: the evening before, when people plan the next day.
REMINDER_LOCAL_HOUR = 17

#: A lead with a visit booked is about to be a customer; an inactive or
#: archived one has been told nothing about a visit that somebody forgot to
#: cancel, and an email is the wrong way for them to find out.
REMINDABLE_CUSTOMER_STATUSES = (CustomerStatus.LEAD, CustomerStatus.ACTIVE)


def reminder_window(
    organization: Organization, now: dt.datetime
) -> tuple[dt.datetime, dt.datetime]:
    """
    The UTC interval of starts that are due a reminder at `now`: open at
    `now` (a visit that has started is past reminding) and closed at the end
    of the last local day whose day-before has reached the reminder hour.

    Before the hour that is the end of today, so a visit later today is still
    due; from the hour on, the end of tomorrow.
    """
    local_now = now.astimezone(organization.tz)
    days_ahead = 1 if local_now.hour >= REMINDER_LOCAL_HOUR else 0
    _, end = organization.local_day_bounds(None, local_now.date() + dt.timedelta(days=days_ahead))
    return now, end


def due_reminders(organization: Organization, now: dt.datetime) -> QuerySet[Job]:
    """
    The visits in `organization` that should be reminded at `now` and whose
    latest reminder, if any, was for a different start.

    Every condition is here and nowhere else, so the tests can walk each
    one. The joins name `deleted_at` themselves: the default manager hides a
    soft-deleted job, not a soft-deleted customer or location behind it.
    """
    if not (organization.is_active and organization.visit_reminders_enabled):
        return Job.objects.none()

    start, end = reminder_window(organization, now)

    latest_reminded_start = Subquery(
        VisitReminder.objects.filter(job=OuterRef("pk"))
        .order_by("-sequence")
        .values("scheduled_start")[:1]
    )

    return (
        Job.objects.filter(
            organization=organization,
            # Only a visit still waiting to happen. A terminal one is over or
            # called off; one en route or in progress is already under way.
            status=JobStatus.SCHEDULED,
            scheduled_start__gt=start,
            scheduled_start__lt=end,
            customer__deleted_at__isnull=True,
            customer__status__in=REMINDABLE_CUSTOMER_STATUSES,
            location__deleted_at__isnull=True,
        )
        .exclude(customer__email="")
        .annotate(reminded_start=latest_reminded_start)
        # Spelled out rather than `~Q(reminded_start=...)`: negating an
        # equality with a NULL side is NULL in SQL, which would drop every
        # visit never reminded at all.
        .filter(
            Q(reminded_start__isnull=True)
            | Q(reminded_start__lt=F("scheduled_start"))
            | Q(reminded_start__gt=F("scheduled_start"))
        )
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
    Record the reminder before sending it. None if it is not this call's to send.

    Not ours when the latest reminder is already for this start, or when
    another run inserts the next `sequence` first: two runs racing for one
    visit both reach the insert, and Postgres lets one through and makes the
    other wait, then raise on the unique constraint.
    """
    latest = VisitReminder.objects.filter(job=job).order_by("-sequence").first()
    if latest is not None and latest.scheduled_start == job.scheduled_start:
        return None
    try:
        with transaction.atomic():
            return VisitReminder.objects.create(
                organization_id=job.organization_id,
                job=job,
                scheduled_start=job.scheduled_start,
                recipient=job.customer.email,
                sequence=(latest.sequence if latest else 0) + 1,
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
