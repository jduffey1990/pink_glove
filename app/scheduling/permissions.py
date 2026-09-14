"""
Assignment-bound permissions.

Lives here rather than in `base.permissions` because it has to import
`scheduling` models, and `base` must not depend on a downstream app.

The shape of `IsAssignedCleaner` is worth understanding before composing with
it. DRF's `|` only consults `has_object_permission` on an operand whose
`has_permission` already passed, so `has_permission` returns True for any staff
member and the real work happens at the object level. In
`[IsDispatcherOrHigher() | IsAssignedCleaner()]`, a dispatcher is admitted by
the left operand outright; a cleaner clears the first gate and is then judged
on whether they actually have a relationship with the object.
"""

import datetime as dt

from django.utils import timezone

from base.permissions import IsOrgMember
from customers.models import ServiceLocation
from scheduling.enums import TERMINAL_STATUSES
from scheduling.models import Job, JobAssignment
from users.enums import DISPATCHER_ROLES, STAFF_ROLES

#: How far either side of a job's window an assignment still counts as "this
#: person has business being here".
#:
#: Deliberately coarse. This permission is a gate against having no
#: relationship at all -- it is not the instrument that decides whether a
#: reveal was appropriately timed. The end-of-day evaluator is, and it works
#: to the organization's own buffers (ADR-016, ADR-017). A tight window here
#: would strand a cleaner over a job that got moved, which is exactly the
#: failure ADR-016 rejected.
ASSIGNMENT_WINDOW = dt.timedelta(hours=24)


class IsAssignedCleaner(IsOrgMember):
    """
    Object-level: the caller has a live assignment tying them to this object.

    Accepts a `Job` (an assignment on it), anything hanging off one such as a
    `JobNote` or `JobPhoto` (judged by its job), or a `ServiceLocation` (an
    assignment on a non-terminal job at that location, inside the 24-hour
    band).
    """

    message = "You are not assigned to this job."

    def has_permission(self, request, view):
        # Any staff member clears this gate; the object check is the real one.
        # Returning False here for non-cleaners would break `|` composition,
        # because DRF then never calls has_object_permission on this operand.
        if not super().has_permission(request, view):
            return False
        if request.user.is_superuser:
            return True

        membership = getattr(request, "membership", None)
        return membership is not None and membership.role in STAFF_ROLES

    def has_object_permission(self, request, view, obj):
        if request.user.is_superuser:
            return True

        membership = getattr(request, "membership", None)
        if membership is None:
            return False
        if membership.role in DISPATCHER_ROLES:
            return True

        if isinstance(obj, Job):
            return self._is_assigned_to(request, obj)

        # JobNote, JobPhoto, TimeEntry -- anything that hangs off a job is
        # judged by that job. Checked before ServiceLocation because these
        # carry a `job`, not a location of their own.
        job = getattr(obj, "job", None)
        if job is not None:
            return self._is_assigned_to(request, job)

        if isinstance(obj, ServiceLocation):
            return self._has_assignment_at_location(request, obj)

        # An object type this permission was never meant to judge. Refusing is
        # the only safe answer: silently passing would grant access by
        # accident the next time it is composed onto a new viewset.
        return False

    @staticmethod
    def _is_assigned_to(request, job) -> bool:
        return JobAssignment.objects.filter(job=job, user=request.user).exists()

    @staticmethod
    def _has_assignment_at_location(request, location) -> bool:
        now = timezone.now()

        return (
            JobAssignment.objects.filter(
                user=request.user,
                job__location=location,
                job__organization=request.organization,
                job__scheduled_start__lte=now + ASSIGNMENT_WINDOW,
                job__scheduled_end__gte=now - ASSIGNMENT_WINDOW,
            )
            .exclude(job__status__in=TERMINAL_STATUSES)
            .exists()
        )


def nearest_job_for_location(*, user, location, organization, now=None) -> Job | None:
    """
    The job that justifies `user` being at `location` right now.

    Used to bind an access reveal to a specific visit (ADR-016). Where several
    assignments qualify -- a cleaner doing a morning and an evening visit at the
    same address -- the one starting nearest to now is the honest answer.
    """
    now = now or timezone.now()

    candidates = (
        Job.objects.filter(
            organization=organization,
            location=location,
            assignments__user=user,
            scheduled_start__lte=now + ASSIGNMENT_WINDOW,
            scheduled_end__gte=now - ASSIGNMENT_WINDOW,
        )
        .exclude(status__in=TERMINAL_STATUSES)
        .distinct()
    )

    return min(candidates, key=lambda job: abs(job.scheduled_start - now), default=None)


def sole_open_job_for_location(*, location, organization, now=None) -> Job | None:
    """
    The single non-terminal job at `location` inside the window, or None if
    there is not exactly one.

    A dispatcher's reveal binds to this when they did not name a job. "Exactly
    one" is the point: guessing between two candidates would put the wrong
    visit on an audit row, and a null job is more honest than a wrong one.
    """
    now = now or timezone.now()

    candidates = list(
        Job.objects.filter(
            organization=organization,
            location=location,
            scheduled_start__lte=now + ASSIGNMENT_WINDOW,
            scheduled_end__gte=now - ASSIGNMENT_WINDOW,
        )
        .exclude(status__in=TERMINAL_STATUSES)
        .distinct()[:2]
    )

    return candidates[0] if len(candidates) == 1 else None
