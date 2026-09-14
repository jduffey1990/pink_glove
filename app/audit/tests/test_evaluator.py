"""
The end-of-day pass over the access trail.

The case worth reading twice is `test_a_late_evening_reveal_waits_for_the_next
_local_day`: at 23:30 in Denver it is already tomorrow in UTC, so an evaluator
that reasoned in UTC would judge that reveal before the day it belongs to was
over -- and a job moved later that evening would then never revise the verdict.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from audit.models import AccessReveal
from audit.tasks import (
    OUTSIDE_BUSINESS_HOURS,
    OUTSIDE_JOB_WINDOW,
    evaluate_access_reveals,
    evaluate_access_reveals_all,
)
from scheduling.tests.factories import JobFactory, UserFactory

DENVER = ZoneInfo("America/Denver")


def _denver(date: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=DENVER)


@pytest.fixture
def yesterday(organization):
    """
    The most recent local *working* day that is already over.

    Not simply "now minus one day": that lands on a weekend two days in seven,
    and the no-job cases below turn on whether the reveal fell inside business
    hours, which a Sunday fails for a reason the test is not about.
    """
    day = timezone.now().astimezone(DENVER).date() - dt.timedelta(days=1)
    while day.isoweekday() > 5:
        day -= dt.timedelta(days=1)
    return day


def _reveal(organization, *, created_at, job=None, user=None, location=None):
    """
    Build a reveal at a specific instant.

    `created_at` is auto_now_add, so it has to be overwritten after the fact.
    """
    location = location or (job.location if job else None)
    if location is None:
        from scheduling.tests.factories import ServiceLocationFactory

        location = ServiceLocationFactory(organization=organization)

    reveal = AccessReveal.objects.create(
        organization=organization,
        user=user or UserFactory(),
        location=location,
        job=job,
        fields_revealed=["gate_code"],
        acknowledged=True,
    )
    AccessReveal.objects.filter(pk=reveal.pk).update(created_at=created_at)
    reveal.refresh_from_db()
    return reveal


@pytest.fixture
def job_yesterday(organization, yesterday):
    """A 09:00-11:00 Denver job on a day that is over."""
    return JobFactory(
        organization=organization,
        scheduled_start=_denver(yesterday, 9),
        scheduled_end=_denver(yesterday, 11),
    )


@pytest.mark.django_db
class TestRevealsAgainstAJobWindow:
    """Defaults: 60 minutes of slack before, 120 after."""

    def test_a_reveal_at_the_door_is_fine(self, organization, yesterday, job_yesterday):
        reveal = _reveal(organization, created_at=_denver(yesterday, 8, 55), job=job_yesterday)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.evaluated_at is not None
        assert not reveal.is_flagged
        assert reveal.flag_reason == ""

    def test_half_an_hour_early_is_inside_the_buffer(self, organization, yesterday, job_yesterday):
        reveal = _reveal(organization, created_at=_denver(yesterday, 8, 30), job=job_yesterday)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert not reveal.is_flagged

    def test_ninety_minutes_early_is_flagged(self, organization, yesterday, job_yesterday):
        reveal = _reveal(organization, created_at=_denver(yesterday, 7, 30), job=job_yesterday)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.is_flagged
        assert reveal.flag_reason == OUTSIDE_JOB_WINDOW

    def test_an_hour_after_the_end_is_inside_the_buffer(
        self, organization, yesterday, job_yesterday
    ):
        """Still finishing up is ordinary; the after-buffer is wider for this reason."""
        reveal = _reveal(organization, created_at=_denver(yesterday, 12, 0), job=job_yesterday)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert not reveal.is_flagged

    def test_three_hours_after_the_end_is_flagged(self, organization, yesterday, job_yesterday):
        reveal = _reveal(organization, created_at=_denver(yesterday, 14, 30), job=job_yesterday)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.is_flagged
        assert reveal.flag_reason == OUTSIDE_JOB_WINDOW

    def test_the_buffers_are_per_organization(self, organization, yesterday, job_yesterday):
        """A tenant whose crews routinely arrive early can widen the window."""
        organization.reveal_buffer_before_minutes = 180
        organization.save()
        reveal = _reveal(organization, created_at=_denver(yesterday, 7, 30), job=job_yesterday)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert not reveal.is_flagged

    def test_the_job_window_beats_business_hours(self, organization, yesterday):
        """
        An overnight job at 2am is inside its own window and outside business
        hours. The job is the more specific instrument, so it wins.
        """
        night_job = JobFactory(
            organization=organization,
            scheduled_start=_denver(yesterday, 2),
            scheduled_end=_denver(yesterday, 4),
        )
        reveal = _reveal(organization, created_at=_denver(yesterday, 2, 5), job=night_job)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert not reveal.is_flagged


@pytest.mark.django_db
class TestRevealsWithNoJob:
    """
    Only a dispatcher+ reveal reaches the evaluator without a job: ADR-017
    refuses a cleaner's outright at request time rather than logging it for
    later review.
    """

    def test_a_daytime_reveal_is_fine(self, organization, yesterday):
        reveal = _reveal(organization, created_at=_denver(yesterday, 10))

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert not reveal.is_flagged

    def test_a_ten_pm_reveal_is_flagged(self, organization, yesterday):
        reveal = _reveal(organization, created_at=_denver(yesterday, 22))

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.is_flagged
        assert reveal.flag_reason == OUTSIDE_BUSINESS_HOURS

    def test_a_weekend_reveal_is_flagged(self, organization):
        """Inside the hours, outside the working week."""
        today_local = timezone.now().astimezone(DENVER).date()
        saturday = today_local - dt.timedelta(days=today_local.isoweekday() + 1)
        assert saturday.isoweekday() == 6

        reveal = _reveal(organization, created_at=_denver(saturday, 10))

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.is_flagged
        assert reveal.flag_reason == OUTSIDE_BUSINESS_HOURS


@pytest.mark.django_db
class TestWhichDaysAreEvaluated:
    def test_a_late_evening_reveal_waits_for_the_next_local_day(self, organization):
        """
        23:30 in Denver is already the next day in UTC. Judging by UTC would
        evaluate this reveal before its own day was over -- and a job moved
        later that evening would then never revise the verdict.
        """
        now_local = timezone.now().astimezone(DENVER)
        tonight = _denver(now_local.date(), 23, 30)
        reveal = _reveal(organization, created_at=tonight)

        # Sanity: the instant really is "tomorrow" in UTC.
        assert tonight.astimezone(dt.UTC).date() > now_local.date()

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.evaluated_at is None

    def test_todays_reveals_are_left_alone(self, organization):
        reveal = _reveal(organization, created_at=timezone.now())

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.evaluated_at is None

    def test_re_running_does_not_touch_already_evaluated_rows(
        self, organization, yesterday, job_yesterday
    ):
        reveal = _reveal(organization, created_at=_denver(yesterday, 8, 55), job=job_yesterday)
        evaluate_access_reveals(str(organization.id))
        reveal.refresh_from_db()
        first_pass = reveal.evaluated_at

        result = evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert result["evaluated"] == 0
        assert reveal.evaluated_at == first_pass

    def test_a_named_day_can_be_re_evaluated(self, organization, yesterday, job_yesterday):
        """
        The schedule was corrected after the fact, so yesterday's verdicts need
        revisiting -- flagged or not.
        """
        reveal = _reveal(organization, created_at=_denver(yesterday, 7, 30), job=job_yesterday)
        evaluate_access_reveals(str(organization.id))
        reveal.refresh_from_db()
        assert reveal.is_flagged

        job_yesterday.scheduled_start = _denver(yesterday, 7, 0)
        job_yesterday.save()
        result = evaluate_access_reveals(str(organization.id), local_date=yesterday.isoformat())

        reveal.refresh_from_db()
        assert result["evaluated"] == 1
        assert not reveal.is_flagged
        assert reveal.flag_reason == ""

    def test_evaluated_at_is_set_on_cleared_rows_too(self, organization, yesterday, job_yesterday):
        """ "Not yet evaluated" and "evaluated and fine" must stay distinguishable."""
        reveal = _reveal(organization, created_at=_denver(yesterday, 8, 55), job=job_yesterday)

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.evaluated_at is not None
        assert not reveal.is_flagged


@pytest.mark.django_db
class TestTenantIsolation:
    def test_one_organizations_run_does_not_touch_anothers_rows(
        self, organization, other_organization, yesterday
    ):
        ours = _reveal(organization, created_at=_denver(yesterday, 22))
        theirs = _reveal(other_organization, created_at=_denver(yesterday, 22))

        evaluate_access_reveals(str(organization.id))

        ours.refresh_from_db()
        theirs.refresh_from_db()
        assert ours.evaluated_at is not None
        assert theirs.evaluated_at is None

    def test_an_unknown_organization_is_a_no_op(self, organization):
        import uuid

        assert evaluate_access_reveals(str(uuid.uuid4())) == {"evaluated": 0, "flagged": 0}

    def test_an_inactive_organization_is_skipped(self, organization, yesterday):
        reveal = _reveal(organization, created_at=_denver(yesterday, 22))
        organization.is_active = False
        organization.save()

        evaluate_access_reveals(str(organization.id))

        reveal.refresh_from_db()
        assert reveal.evaluated_at is None


@pytest.mark.django_db
class TestFanOut:
    def test_it_dispatches_one_task_per_active_organization(
        self, organization, other_organization, yesterday
    ):
        """Tasks run eagerly under test settings, so this evaluates for real."""
        ours = _reveal(organization, created_at=_denver(yesterday, 22))
        # The rival keeps its own timezone (New York), so this is dated well
        # back rather than reusing a Denver wall-clock time that may still be
        # "today" for them.
        theirs = _reveal(other_organization, created_at=timezone.now() - dt.timedelta(days=3))

        queued = evaluate_access_reveals_all()

        ours.refresh_from_db()
        theirs.refresh_from_db()
        assert queued == 2
        assert ours.is_flagged
        assert theirs.evaluated_at is not None

    def test_inactive_organizations_are_not_dispatched(self, organization, other_organization):
        other_organization.is_active = False
        other_organization.save()

        assert evaluate_access_reveals_all() == 1
