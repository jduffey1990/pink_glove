"""
The end-of-day pass over the access trail.

Why this runs after the fact rather than blocking at reveal time: whether a
reveal sat inside its appointment window depends on how the schedule finally
stood that day. A job moved at 4pm changes the verdict on a 2pm reveal, and
blocking in the moment would strand a worker over a scheduling change that was
not their fault (ADR-016).

Scheduled hourly, not daily. Each run evaluates only reveals whose
organization-local date is already over, which comes to "once per day after
close" for every tenant regardless of timezone -- without a per-organization
cron entry.
"""

import datetime as dt
import logging

from celery import shared_task
from django.utils import timezone

from audit.models import AccessReveal
from organizations.models import Organization

logger = logging.getLogger(__name__)

#: Why a reveal was flagged. Stored on the row, shown to a reviewing admin.
OUTSIDE_JOB_WINDOW = "outside_job_window"
OUTSIDE_BUSINESS_HOURS = "outside_business_hours"


@shared_task(name="audit.evaluate_access_reveals_all")
def evaluate_access_reveals_all() -> int:
    organization_ids = Organization.objects.filter(is_active=True).values_list("id", flat=True)

    for organization_id in organization_ids:
        evaluate_access_reveals.delay(str(organization_id))

    return len(organization_ids)


@shared_task(name="audit.evaluate_access_reveals")
def evaluate_access_reveals(organization_id: str, local_date: str | None = None) -> dict:
    """
    Judge one organization's unevaluated reveals. Returns counts.

    Normally evaluates every unevaluated reveal from a local day that is
    already over. Passing `local_date` (an ISO date) re-runs that specific day
    instead, re-evaluating rows that were already judged -- which is what an
    admin needs after correcting a schedule that made yesterday's verdicts
    wrong.
    """
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        logger.warning("evaluate_access_reveals: no active organization %s", organization_id)
        return {"evaluated": 0, "flagged": 0}

    tz = organization.tz
    reveals = AccessReveal.objects.filter(organization=organization).select_related(
        "job", "organization"
    )

    if local_date:
        target = dt.date.fromisoformat(local_date)
        day_start = dt.datetime.combine(target, dt.time.min, tzinfo=tz)
        day_end = dt.datetime.combine(target, dt.time.max, tzinfo=tz)
        reveals = reveals.filter(created_at__gte=day_start, created_at__lte=day_end)
    else:
        # Only days that are over locally. A reveal made at 23:30 local is not
        # judged until the next local day, even though in UTC it may already
        # look like yesterday.
        today_local = timezone.now().astimezone(tz).date()
        cutoff = dt.datetime.combine(today_local, dt.time.min, tzinfo=tz)
        reveals = reveals.filter(evaluated_at__isnull=True, created_at__lt=cutoff)

    evaluated = flagged = 0
    now = timezone.now()

    for reveal in reveals:
        is_flagged, reason = _judge(reveal, organization)
        reveal.evaluated_at = now
        reveal.is_flagged = is_flagged
        reveal.flag_reason = reason
        # Not bulk_update: the trail is small (one row per door opened) and a
        # per-row save keeps `updated_at` honest.
        reveal.save(update_fields=["evaluated_at", "is_flagged", "flag_reason", "updated_at"])
        evaluated += 1
        flagged += int(is_flagged)

    return {"evaluated": evaluated, "flagged": flagged}


def _judge(reveal: AccessReveal, organization) -> tuple[bool, str]:
    """
    Flag or clear one reveal.

    A reveal bound to a job is judged against that job's window plus the
    organization's buffers. One without a job can only be a dispatcher's
    (ADR-017 refuses a cleaner's outright at request time), so the looser
    business-hours test is the right instrument.
    """
    if reveal.job_id is not None:
        job = reveal.job
        before = dt.timedelta(minutes=organization.reveal_buffer_before_minutes)
        after = dt.timedelta(minutes=organization.reveal_buffer_after_minutes)

        too_early = reveal.created_at < job.scheduled_start - before
        too_late = reveal.created_at > job.scheduled_end + after

        if too_early or too_late:
            return True, OUTSIDE_JOB_WINDOW
        return False, ""

    if not organization.is_within_business_hours(reveal.created_at):
        return True, OUTSIDE_BUSINESS_HOURS

    return False, ""
