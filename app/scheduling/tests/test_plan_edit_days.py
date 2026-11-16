"""
Editing a plan must not put a second visit on a day that already has one.

ADR-020 keeps a visit a dispatcher moved, cancelled or worked, and finds it
again by `plan_occurrence`: the instant it was planned for. That holds until
the plan's time changes. Then every occurrence is a different instant, the
kept visit no longer accounts for its day, and the rebuild puts a fresh visit
beside it. The visit the customer asked to move turns up twice, and the one
they cancelled comes back (ADR-021's failure by another road).

`regenerate_plan` now hands each kept visit the new definition's occurrence
on the local day it was planned for (ADR-032). These tests are the
reproduction: each one failed before that.
"""

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from scheduling.enums import JobStatus
from scheduling.models import Job
from scheduling.services import materialize_plan, regenerate_plan
from scheduling.tests.factories import RecurringPlanFactory, TimeEntryFactory


def detail(job):
    return reverse("scheduling:job-detail", args=[job.id])


def plan_url(plan):
    return reverse("scheduling:plan-detail", args=[plan.id])


def planned_day(job):
    return job.plan_occurrence.astimezone(job.organization.tz).date()


def visits_planned_for(plan, day):
    """Live visits of `plan` whose occurrence falls on local `day`."""
    return [
        job
        for job in Job.objects.filter(plan=plan).order_by("scheduled_start")
        if planned_day(job) == day
    ]


def move(client, job, *, by):
    start = job.scheduled_start + by
    return client.patch(
        detail(job),
        {
            "scheduled_start": start.isoformat(),
            "scheduled_end": (start + dt.timedelta(hours=2)).isoformat(),
        },
        format="json",
    )


def retime(client, plan, time="14:00:00"):
    return client.patch(plan_url(plan), {"preferred_start_time": time}, format="json")


@pytest.fixture
def plan(organization, customer, location, service):
    plan = RecurringPlanFactory(
        organization=organization,
        customer=customer,
        location=location,
        service=service,
        rrule="FREQ=DAILY",
        starts_on=organization.today(),
        preferred_start_time=dt.time(9, 0),
        duration_minutes=120,
    )
    materialize_plan(plan)
    return plan


@pytest.fixture
def visit(plan):
    """A visit a week out, so nothing about it is close to `now`."""
    cutoff = timezone.now() + dt.timedelta(days=7)
    return (
        Job.objects.filter(plan=plan, scheduled_start__gt=cutoff)
        .order_by("scheduled_start")
        .first()
    )


@pytest.mark.django_db
class TestAKeptVisitStillAccountsForItsDay:
    def test_a_moved_visit_is_not_joined_by_a_new_one(self, dispatcher_client, plan, visit):
        day = planned_day(visit)
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=2))

        body = retime(dispatcher_client, plan).json()

        assert body["kept"] == 1
        assert [job.pk for job in visits_planned_for(plan, day)] == [visit.pk]

    def test_a_cancelled_visit_does_not_come_back(self, dispatcher_client, plan, visit):
        day = planned_day(visit)
        dispatcher_client.post(
            reverse("scheduling:job-status", args=[visit.id]),
            {"status": "cancelled", "reason": "Customer is away."},
            format="json",
        )

        retime(dispatcher_client, plan)

        assert [job.status for job in visits_planned_for(plan, day)] == [JobStatus.CANCELLED]

    def test_a_worked_visit_is_not_joined_by_a_new_one(
        self, dispatcher_client, organization, plan, visit, cleaner
    ):
        day = planned_day(visit)
        TimeEntryFactory(organization=organization, job=visit, user=cleaner)

        retime(dispatcher_client, plan)

        assert [job.pk for job in visits_planned_for(plan, day)] == [visit.pk]

    def test_tonights_run_does_not_add_one_either(self, dispatcher_client, plan, visit):
        day = planned_day(visit)
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=2))
        retime(dispatcher_client, plan)
        plan.refresh_from_db()

        assert materialize_plan(plan) == 0
        assert [job.pk for job in visits_planned_for(plan, day)] == [visit.pk]

    def test_a_second_edit_still_finds_it(self, dispatcher_client, plan, visit):
        day = planned_day(visit)
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=2))

        retime(dispatcher_client, plan, "14:00:00")
        retime(dispatcher_client, plan, "08:30:00")

        assert [job.pk for job in visits_planned_for(plan, day)] == [visit.pk]

    def test_the_moved_visit_itself_is_left_where_the_dispatcher_put_it(
        self, dispatcher_client, plan, visit
    ):
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=2))
        visit.refresh_from_db()
        moved_to = visit.scheduled_start

        retime(dispatcher_client, plan)

        visit.refresh_from_db()
        assert visit.deleted_at is None
        assert visit.scheduled_start == moved_to

    def test_every_other_day_still_gets_exactly_one_visit_at_the_new_time(
        self, dispatcher_client, plan, visit
    ):
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=2))

        retime(dispatcher_client, plan)

        tz = plan.organization.tz
        future = Job.objects.filter(plan=plan, scheduled_start__gt=timezone.now())
        days = [planned_day(job) for job in future]
        assert len(days) == len(set(days))
        assert all(
            job.scheduled_start.astimezone(tz).hour == 14 for job in future if job.pk != visit.pk
        )

    def test_todays_finished_visit_is_not_followed_by_another_today(self, organization, plan):
        """
        The morning visit is done. Moving the plan to the afternoon at noon
        must not send the crew back the same day.
        """
        today = organization.today()
        done = visits_planned_for(plan, today)[0]
        Job.objects.filter(pk=done.pk).update(
            status=JobStatus.COMPLETE,
            scheduled_start=timezone.now() - dt.timedelta(hours=3),
            scheduled_end=timezone.now() - dt.timedelta(hours=1),
        )

        plan.preferred_start_time = dt.time(23, 59)
        plan.save()
        regenerate_plan(plan)

        assert [job.pk for job in visits_planned_for(plan, today)] == [done.pk]


@pytest.mark.django_db
class TestWhatTheRekeyLeavesAlone:
    def test_an_edit_that_keeps_the_time_changes_no_keys(self, dispatcher_client, plan, visit):
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=2))
        planned_for = visit.plan_occurrence

        dispatcher_client.patch(plan_url(plan), {"duration_minutes": 180}, format="json")

        visit.refresh_from_db()
        assert visit.plan_occurrence == planned_for

    def test_a_visit_whose_day_the_new_rule_drops_keeps_its_old_key(
        self, dispatcher_client, organization, customer, location, service
    ):
        """
        Tuesdays become Thursdays. A Tuesday visit that was moved has no
        Thursday of its own to claim, so it is kept as it is and the Thursday
        series is built beside it. `kept` in the response is what tells the
        dispatcher to go and look.
        """
        weekly = RecurringPlanFactory(
            organization=organization,
            customer=customer,
            location=location,
            service=service,
            rrule="FREQ=WEEKLY;BYDAY=TU",
            starts_on=organization.today(),
        )
        materialize_plan(weekly)
        tuesday = Job.objects.filter(plan=weekly).order_by("scheduled_start")[2]
        planned_for = tuesday.plan_occurrence
        move(dispatcher_client, tuesday, by=dt.timedelta(hours=2))

        body = dispatcher_client.patch(
            plan_url(weekly), {"rrule": "FREQ=WEEKLY;BYDAY=TH"}, format="json"
        ).json()

        tuesday.refresh_from_db()
        assert body["kept"] == 1
        assert tuesday.deleted_at is None
        assert tuesday.plan_occurrence == planned_for

    def test_a_paused_plan_rekeys_nothing(self, dispatcher_client, plan, visit):
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=2))
        planned_for = visit.plan_occurrence

        dispatcher_client.patch(
            plan_url(plan), {"is_active": False, "preferred_start_time": "14:00:00"}, format="json"
        )

        visit.refresh_from_db()
        assert visit.plan_occurrence == planned_for

    def test_running_it_twice_gives_the_same_board(self, dispatcher_client, plan, visit):
        """Unordered reads and a tie-break sit behind this, so: twice, and diff."""
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=2))
        retime(dispatcher_client, plan)
        plan.refresh_from_db()

        def board():
            return sorted(
                Job.objects.filter(plan=plan).values_list(
                    "plan_occurrence", "scheduled_start", "status"
                )
            )

        first = board()
        result = regenerate_plan(plan)
        second = board()

        assert first == second
        assert result["kept"] == 1
