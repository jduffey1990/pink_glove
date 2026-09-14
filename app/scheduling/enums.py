from django.db.models import TextChoices


class JobStatus(TextChoices):
    """
    Where a visit stands.

    NO_ACCESS is not padding. "We turned up and could not get in" is a
    routine, distinct outcome: it is not a cancellation (the crew was
    dispatched and the slot was consumed) and it is not a completion, and it
    bills differently from both.
    """

    SCHEDULED = "scheduled", "Scheduled"
    EN_ROUTE = "en_route", "En route"
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETE = "complete", "Complete"
    CANCELLED = "cancelled", "Cancelled"
    NO_ACCESS = "no_access", "No access"


#: Statuses a job does not move on from by itself. Reopening one is a
#: deliberate dispatcher action, not part of the normal forward flow.
TERMINAL_STATUSES = (JobStatus.COMPLETE, JobStatus.CANCELLED, JobStatus.NO_ACCESS)

#: The state machine, enforced in `scheduling.services.transition_job`.
#: Reopening a terminal job back to SCHEDULED is handled separately because it
#: is dispatcher-only.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    JobStatus.SCHEDULED: (
        JobStatus.EN_ROUTE,
        JobStatus.IN_PROGRESS,
        JobStatus.CANCELLED,
        JobStatus.NO_ACCESS,
    ),
    JobStatus.EN_ROUTE: (
        JobStatus.IN_PROGRESS,
        JobStatus.SCHEDULED,
        JobStatus.CANCELLED,
        JobStatus.NO_ACCESS,
    ),
    JobStatus.IN_PROGRESS: (JobStatus.COMPLETE, JobStatus.NO_ACCESS),
    JobStatus.COMPLETE: (JobStatus.SCHEDULED,),
    JobStatus.CANCELLED: (JobStatus.SCHEDULED,),
    JobStatus.NO_ACCESS: (JobStatus.SCHEDULED,),
}

#: Statuses whose transition requires a written reason.
REASON_REQUIRED_STATUSES = (JobStatus.CANCELLED, JobStatus.NO_ACCESS)
