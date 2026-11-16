"""
Scheduling business logic. Views stay thin; this is where the rules live.

The recurrence half of this module has one idea worth holding on to: a plan is
authored in *local wall-clock* time ("every Tuesday at 9am") and stored in UTC.
Those are not the same thing across a DST boundary, so expansion happens naive,
in the organization's zone, and converts to UTC only at the end. See ADR-012
and `scheduling/tests/test_recurrence.py`.
"""

import datetime as dt

from django.db import models, transaction
from django.utils import timezone

from app.exceptions import ConflictError
from scheduling.enums import ALLOWED_TRANSITIONS, REASON_REQUIRED_STATUSES, JobStatus
from scheduling.models import Job, JobAssignment, RecurringPlan, TimeEntry

#: How far ahead jobs are created. Eight weeks is far enough that a dispatcher
#: can plan a rota against real rows, and near enough that a plan edit does not
#: churn a year of them. ADR-012.
MATERIALIZATION_HORIZON = dt.timedelta(days=56)


def organization_today(organization) -> dt.date:
    """
    The organization's current local date, which is the one it schedules by.

    Kept as a module-level alias because the recurrence code reads better with
    it; the rule itself lives on `Organization` so billing and the audit pass
    reckon "today" the same way.
    """
    return organization.today()


def expand_occurrences(
    plan: RecurringPlan, *, window_start: dt.date, window_end: dt.date
) -> list[dt.datetime]:
    """
    Local wall-clock occurrences of `plan` within [window_start, window_end],
    returned as aware UTC datetimes. Both ends of the window are inclusive.

    The order of operations is the load-bearing part:

    1. Build DTSTART as a *naive* local datetime from `starts_on` and
       `preferred_start_time`.
    2. Expand the RRULE naive, so the rule only ever sees wall-clock time.
    3. Attach the organization's zone, then convert to UTC.

    Expanding in UTC instead would shift every occurrence by an hour for half
    the year -- "every Tuesday 9am" would become 8am or 10am the moment the
    clocks changed.

    A wall-clock time that does not exist on spring-forward day (02:30) or
    happens twice on fall-back day (01:30) is resolved by `fold=0`: the
    pre-transition offset. For the nonexistent case that means the instant
    lands just after the gap; for the ambiguous case, the first pass.
    """
    from dateutil.rrule import rrulestr

    if window_end < window_start:
        return []

    tz = plan.organization.tz

    # Step 1: naive local DTSTART.
    dtstart = dt.datetime.combine(plan.starts_on, plan.preferred_start_time)

    # The rule is expanded over a naive window too. `between` is exclusive at
    # both ends, so widen by a second either side to make the day-level window
    # inclusive as documented.
    naive_from = dt.datetime.combine(window_start, dt.time.min) - dt.timedelta(seconds=1)
    naive_to = dt.datetime.combine(window_end, dt.time.max) + dt.timedelta(seconds=1)

    if plan.ends_on is not None:
        ends_at = dt.datetime.combine(plan.ends_on, dt.time.max)
        naive_to = min(naive_to, ends_at)
        if naive_to < naive_from:
            return []

    # Step 2: expand naive.
    rule = rrulestr(plan.rrule.strip(), dtstart=dtstart)
    naive_occurrences = rule.between(naive_from, naive_to, inc=False)

    # Step 3: localize, then convert. fold=0 is explicit rather than implied.
    return [
        occurrence.replace(tzinfo=tz, fold=0).astimezone(dt.UTC) for occurrence in naive_occurrences
    ]


def quote_job_price(*, plan: RecurringPlan | None, service, location, duration_minutes: int) -> int:
    """
    What this visit is worth, in cents, at the moment it is created.

    A plan's `price_override_cents` wins outright -- it is the negotiated rate
    for that standing slot, and re-quoting it would quietly undo the
    negotiation. Otherwise the service prices itself against the location.

    Raises ValueError (not a 500) when the pricing model's input is missing;
    callers surface it as a 400 naming the field.
    """
    if plan is not None and plan.price_override_cents is not None:
        return plan.price_override_cents

    from decimal import Decimal

    return service.quote_cents(
        square_feet=location.square_feet,
        hours=Decimal(duration_minutes) / Decimal(60),
    )


@transaction.atomic
def materialize_plan(plan: RecurringPlan, *, today: dt.date | None = None) -> int:
    """
    Create any missing `Job` rows for `plan` out to the horizon.

    Returns the number created. Safe to run repeatedly -- the daily beat task
    does exactly that, and the unique constraint on (plan, plan_occurrence) is
    what makes it idempotent.

    `today` defaults to the organization's own current date and is a parameter
    so tests can pin it.
    """
    if not plan.is_active:
        return 0

    organization = plan.organization
    today = today or organization_today(organization)

    window_start = max(today, plan.starts_on)
    window_end = today + MATERIALIZATION_HORIZON
    if plan.ends_on is not None:
        window_end = min(window_end, plan.ends_on)

    if window_end < window_start:
        return 0

    occurrences = expand_occurrences(plan, window_start=window_start, window_end=window_end)
    if not occurrences:
        return 0

    # Live rows only, deliberately. The unique constraint is itself partial on
    # `deleted_at is null`, and regeneration works by soft-deleting untouched
    # jobs and letting this rebuild them (ADR-020) -- counting soft-deleted
    # rows as "existing" would make regenerate_plan a no-op that deletes.
    #
    # The consequence: soft-deleting a job does not suppress the visit, because
    # the next daily run recreates it. Cancelling is what suppresses it -- a
    # CANCELLED job is a live row at that occurrence, so it blocks re-creation
    # and keeps the reason on the record. That is why the API refuses to
    # destroy a worked job and tells the caller to cancel instead.
    existing = set(
        Job.objects.filter(plan=plan, plan_occurrence__in=occurrences).values_list(
            "plan_occurrence", flat=True
        )
    )
    missing = [o for o in occurrences if o not in existing]
    if not missing:
        return 0

    duration = dt.timedelta(minutes=plan.duration_minutes)
    price_cents = quote_job_price(
        plan=plan,
        service=plan.service,
        location=plan.location,
        duration_minutes=plan.duration_minutes,
    )

    jobs = [
        Job(
            organization=organization,
            customer_id=plan.customer_id,
            location_id=plan.location_id,
            service_id=plan.service_id,
            plan=plan,
            plan_occurrence=occurrence,
            scheduled_start=occurrence,
            scheduled_end=occurrence + duration,
            status=JobStatus.SCHEDULED,
            price_cents=price_cents,
            notes=plan.notes,
        )
        for occurrence in missing
    ]

    # bulk_create bypasses TenantModel.save(), and with it the
    # cross-organization FK check. Acceptable *here only*: every FK is copied
    # straight off the plan, which was validated on its own save. Do not reuse
    # this shortcut for rows whose FKs come from anywhere else.
    created = Job.objects.bulk_create(jobs, ignore_conflicts=True)

    _assign_plan_defaults(plan, created)

    return len(created)


def _assign_plan_defaults(plan: RecurringPlan, jobs: list[Job]) -> None:
    """Put the plan's standing crew on each newly created job."""
    assignee_ids = list(plan.default_assignees.values_list("id", flat=True))
    if not assignee_ids or not jobs:
        return

    # ignore_conflicts because bulk_create above may have silently skipped a
    # row that raced us, leaving a Job instance here with no database row.
    JobAssignment.objects.bulk_create(
        [
            JobAssignment(organization=plan.organization, job=job, user_id=user_id)
            for job in jobs
            if job.pk is not None
            for user_id in assignee_ids
        ],
        ignore_conflicts=True,
    )


def _untouched_future_jobs(plan: RecurringPlan, *, now: dt.datetime | None = None):
    """
    Future jobs from `plan` that nobody has touched.

    "Untouched" is derived from state that is already there rather than from a
    flag someone has to remember to set (ADR-020): still SCHEDULED, still
    sitting at the instant the plan put it, and never worked.
    """
    now = now or timezone.now()

    return (
        Job.objects.filter(
            plan=plan,
            scheduled_start__gt=now,
            status=JobStatus.SCHEDULED,
            plan_occurrence__isnull=False,
        )
        .filter(scheduled_start=models.F("plan_occurrence"))
        .exclude(time_entries__isnull=False)
    )


@transaction.atomic
def regenerate_plan(plan: RecurringPlan, *, today: dt.date | None = None) -> dict:
    """
    Re-materialize `plan` after an edit. Returns {"regenerated": n, "kept": m}.

    Untouched future jobs are soft-deleted and rebuilt from the plan's current
    definition. Every other future job -- rescheduled, already started, worked,
    cancelled -- is kept exactly as it is and stays linked to the plan. See
    ADR-020 for why the alternatives were rejected.

    Deactivating a plan runs the same drop step with no rebuild, which is what
    `is_active = False` means here.
    """
    now = timezone.now()

    future = Job.objects.filter(plan=plan, scheduled_start__gt=now)
    untouched = list(_untouched_future_jobs(plan, now=now).values_list("pk", flat=True))
    kept = future.exclude(pk__in=untouched).count()

    if untouched:
        Job.objects.filter(pk__in=untouched).delete()  # soft delete

    regenerated = 0
    if plan.is_active:
        _rekey_kept_jobs(plan, today=today or organization_today(plan.organization))
        regenerated = materialize_plan(plan, today=today)

    return {"regenerated": regenerated, "kept": kept}


def _rekey_kept_jobs(plan: RecurringPlan, *, today: dt.date) -> None:
    """
    Give each visit that survived a plan edit the occurrence the plan's
    *current* definition has on the local day it was planned for (ADR-032).

    `plan_occurrence` is an instant, and it is how the materializer knows an
    occurrence already has its visit. Move the plan from 9am to 2pm and every
    occurrence is a different instant: a kept visit, still keyed to 9am, no
    longer accounts for its day, so the rebuild puts a new 2pm visit beside
    it. The visit a customer asked to move turns up twice, and the one they
    cancelled comes back. Re-keying to the same local day is what makes
    "kept" mean "this is still that day's visit".

    Runs between the drop and the rebuild, so the only live rows are the kept
    ones and no key it hands out can collide with an untouched job. A visit
    whose day the new definition no longer has (Tuesdays became Thursdays)
    has nothing to claim and keeps its old key.

    Reaches back to the start of today, not just to now: this morning's
    finished visit must still account for today when the plan moves to the
    afternoon.
    """
    tz = plan.organization.tz
    occurrences = expand_occurrences(
        plan, window_start=today, window_end=today + MATERIALIZATION_HORIZON
    )
    if not occurrences:
        return

    current = set(occurrences)
    by_day: dict[dt.date, list[dt.datetime]] = {}
    for occurrence in occurrences:
        by_day.setdefault(occurrence.astimezone(tz).date(), []).append(occurrence)

    # Ordered, so which visit claims which occurrence is the same on every run.
    kept = list(
        Job.objects.filter(
            plan=plan,
            plan_occurrence__gte=dt.datetime.combine(today, dt.time.min, tzinfo=tz),
        ).order_by("plan_occurrence", "pk")
    )
    taken = {job.plan_occurrence for job in kept}

    for job in kept:
        if job.plan_occurrence in current:
            continue

        planned_day = job.plan_occurrence.astimezone(tz).date()
        free = [o for o in by_day.get(planned_day, ()) if o not in taken]
        if not free:
            continue

        taken.add(free[0])
        job.plan_occurrence = free[0]
        job.save(update_fields=["plan_occurrence", "updated_at"])


# ---------------------------------------------------------------------------
# Job actions
# ---------------------------------------------------------------------------
# These raise Django's ValidationError for "you gave me bad input" (rendered as
# a 400 by app.exceptions, ADR-015) and `app.exceptions.ConflictError` for "the
# record is not in a state where that makes sense" (409). Both are rendered by
# the handler, so no view catches either. The distinction matters to the
# frontend: a 400 means fix the payload, a 409 means re-read the job.


def assert_can_be_assigned(*, user, organization) -> None:
    """
    A job may only be assigned to staff of its own organization.

    `JobAssignment.user` is a `CustomUser`, which is not a `TenantModel`, so
    `TenantModel._check_tenant_consistency` does not cover this edge -- without
    the check, a rival organization's cleaner id in a payload would be accepted.
    """
    from django.core.exceptions import ValidationError

    from users.enums import STAFF_ROLES
    from users.models import Membership

    is_staff_here = Membership.objects.filter(
        user=user, organization=organization, role__in=STAFF_ROLES, is_active=True
    ).exists()

    if not is_staff_here:
        raise ValidationError({"user": "That user is not active staff in this organization."})


@transaction.atomic
def assign_user(*, job: Job, user, assigned_by=None) -> JobAssignment:
    """Put `user` on `job`. Idempotent: re-assigning returns the existing row."""
    if job.is_terminal:
        raise ConflictError(
            f"This job is {job.get_status_display().lower()} and cannot be assigned.",
            {"status": job.status},
        )

    assert_can_be_assigned(user=user, organization=job.organization)

    assignment, _ = JobAssignment.objects.get_or_create(
        job=job,
        user=user,
        defaults={"organization": job.organization, "assigned_by": assigned_by},
    )
    return assignment


@transaction.atomic
def unassign_user(*, job: Job, user) -> bool:
    """Take `user` off `job`. Returns whether anything was removed."""
    assignment = JobAssignment.objects.filter(job=job, user=user).first()
    if assignment is None:
        return False

    assignment.delete()
    return True


#: What a dispatcher may do to a visit besides move its status. Published on
#: the job as `available_actions` and enforced by `JobViewSet` from the same
#: two functions below, so a page never decides either one (ADR-023, ADR-032).
ACTION_EDIT = "edit"
ACTION_DELETE = "delete"
JOB_ACTIONS = (ACTION_EDIT, ACTION_DELETE)


def edit_refusal(job: Job) -> str | None:
    """
    Why `job` may not be edited right now, or None if it may.

    A finished visit is the record of what happened: time entries, an invoice
    line and its dated description may all point at it. Changing one is a
    correction, and the state machine already has the deliberate step for
    that, so the answer is "reopen it first" rather than a quiet rewrite.
    `assign_user` refuses a finished job for the same reason.
    """
    if job.is_terminal:
        return (
            f"This job is {job.get_status_display().lower()} and cannot be changed. "
            "Reopen it first."
        )
    return None


def delete_refusal(job: Job) -> str | None:
    """
    Why `job` may not be deleted right now, or None if it may.

    Deletion is for a visit booked by mistake that nothing has happened to
    yet (ADR-021). Everything else is called off by cancelling, which keeps
    the row and the reason:

    * recorded time would be orphaned, along with the hours billed against it;
    * a visit that is under way or finished is history, not a mistake;
    * a visit a plan created comes straight back, because the materializer
      only counts live rows. Deleting a *cancelled* one is the worst case: it
      puts the visit the customer called off back on the board.
    """
    if job.time_entries.exists():
        return "This job has recorded time. Cancel it instead of deleting it."

    if job.status != JobStatus.SCHEDULED:
        return (
            "Only a job that is still scheduled can be deleted. "
            f"This one is {job.get_status_display().lower()}."
        )

    if job.plan_occurrence is not None:
        return (
            "This visit comes from a recurring plan, so deleting it would only "
            "bring it back. Cancel it instead."
        )

    return None


def job_actions(job: Job, *, is_dispatcher: bool) -> list[str]:
    """Which of `JOB_ACTIONS` the server will accept for `job` from this caller."""
    if not is_dispatcher:
        return []

    refusals = {ACTION_EDIT: edit_refusal, ACTION_DELETE: delete_refusal}
    return [action for action in JOB_ACTIONS if refusals[action](job) is None]


def next_statuses(job: Job, *, is_dispatcher: bool) -> list[str]:
    """
    Where `job` may go next, for this kind of caller.

    The one statement of the role rules on top of `ALLOWED_TRANSITIONS`:
    reopening a finished visit and cancelling one are dispatcher decisions.
    `transition_job` enforces it and `JobSerializer` publishes it, so the
    buttons a page draws and the moves the server accepts cannot drift.
    """
    allowed = ALLOWED_TRANSITIONS.get(job.status, ())
    if is_dispatcher:
        return list(allowed)
    if job.is_terminal:
        return []
    return [status for status in allowed if status != JobStatus.CANCELLED]


@transaction.atomic
def transition_job(*, job: Job, to_status: str, actor, reason: str = "") -> Job:
    """
    Move `job` to `to_status`, enforcing the state machine in
    `scheduling.enums.ALLOWED_TRANSITIONS`.

    Enforced here rather than in the view because three callers need it: the
    status action, clock-in (which drags a job into IN_PROGRESS), and the
    seed command.
    """
    from django.core.exceptions import ValidationError

    if to_status not in JobStatus.values:
        raise ValidationError({"status": f"{to_status!r} is not a job status."})

    allowed = ALLOWED_TRANSITIONS.get(job.status, ())
    if to_status not in allowed:
        raise ConflictError(
            f"A {job.get_status_display().lower()} job cannot become "
            f"{JobStatus(to_status).label.lower()}.",
            {"status": job.status, "allowed": list(allowed)},
        )

    is_dispatcher = _is_dispatcher_or_higher(actor, job.organization)
    permitted = next_statuses(job, is_dispatcher=is_dispatcher)

    if to_status not in permitted:
        # Legal for the machine, not for this caller. Reopening a finished
        # visit is a correction, and cancelling is a business decision.
        message = (
            "Only a dispatcher can reopen a job that is already finished."
            if job.is_terminal
            else "Only a dispatcher can cancel a job."
        )
        raise ConflictError(message, {"status": job.status, "allowed": permitted})

    if to_status in REASON_REQUIRED_STATUSES and not (reason or "").strip():
        raise ValidationError(
            {"reason": f"A reason is required to mark a job {JobStatus(to_status).label.lower()}."}
        )

    job.status = to_status
    job.status_changed_at = timezone.now()
    fields = ["status", "status_changed_at", "updated_at"]

    if to_status in REASON_REQUIRED_STATUSES:
        job.cancellation_reason = reason.strip()[:255]
        fields.append("cancellation_reason")

    job.save(update_fields=fields)
    return job


def _is_dispatcher_or_higher(user, organization) -> bool:
    from users.enums import DISPATCHER_ROLES
    from users.models import Membership

    if user is None:
        return False
    if getattr(user, "is_superuser", False):
        return True

    return Membership.objects.filter(
        user=user, organization=organization, role__in=DISPATCHER_ROLES, is_active=True
    ).exists()


@transaction.atomic
def clock_in(*, job: Job, user) -> TimeEntry:
    """
    Start `user`'s stretch of work on `job`.

    Moves a SCHEDULED or EN_ROUTE job to IN_PROGRESS as a side effect: someone
    is on site with the clock running, and making the cleaner press two buttons
    to say so is how statuses end up wrong.
    """
    if job.is_terminal:
        raise ConflictError(
            f"This job is {job.get_status_display().lower()}; you cannot clock in.",
            {"status": job.status},
        )

    if TimeEntry.objects.filter(job=job, user=user, clock_out__isnull=True).exists():
        raise ConflictError("You are already clocked in to this job.")

    entry = TimeEntry.objects.create(
        organization=job.organization, job=job, user=user, clock_in=timezone.now()
    )

    if job.status in (JobStatus.SCHEDULED, JobStatus.EN_ROUTE):
        job.status = JobStatus.IN_PROGRESS
        job.status_changed_at = timezone.now()
        job.save(update_fields=["status", "status_changed_at", "updated_at"])

    return entry


@transaction.atomic
def clock_out(*, job: Job, user) -> TimeEntry:
    """Close `user`'s open stretch on `job`. Leaves the status alone -- whether
    the visit is complete is a separate judgement the cleaner makes."""
    entry = TimeEntry.objects.filter(job=job, user=user, clock_out__isnull=True).first()
    if entry is None:
        raise ConflictError("You are not clocked in to this job.")

    entry.clock_out = timezone.now()
    entry.save(update_fields=["clock_out", "updated_at"])
    return entry
