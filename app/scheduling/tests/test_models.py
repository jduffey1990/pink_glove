"""
Scheduling model invariants -- the ones the database, not a view, must hold.

Every check here is something a bug elsewhere could otherwise get past: a job
pointing at a rival tenant's location, a plan re-materializing a duplicate
occurrence, two open clock-ins for the same person on the same job.
"""

import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from scheduling.enums import ALLOWED_TRANSITIONS, TERMINAL_STATUSES, JobStatus
from scheduling.models import Job, TimeEntry, validate_rrule
from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    RecurringPlanFactory,
    ServiceLocationFactory,
    TimeEntryFactory,
    UserFactory,
)


@pytest.mark.django_db
class TestJobTenantConsistency:
    def test_a_job_cannot_point_at_another_organizations_location(
        self, organization, other_organization
    ):
        """
        Queryset scoping stops a caller *reading* the rival's location. It does
        not stop them naming its id in a write -- this does.
        """
        ours = CustomerFactory(organization=organization)
        theirs = ServiceLocationFactory(organization=other_organization)

        job = JobFactory.build(
            organization=organization,
            customer=ours,
            location=theirs,
            service=JobFactory.service.get_factory()(organization=organization),
        )

        with pytest.raises(ValidationError) as excinfo:
            job.save()

        assert "location" in excinfo.value.message_dict

    def test_a_location_from_a_different_customer_is_rejected(self, organization):
        job = JobFactory.build(
            organization=organization,
            customer=CustomerFactory(organization=organization),
            location=ServiceLocationFactory(organization=organization),
            service=JobFactory.service.get_factory()(organization=organization),
            scheduled_start=timezone.now(),
            scheduled_end=timezone.now() + dt.timedelta(hours=2),
            price_cents=100,
        )

        with pytest.raises(ValidationError) as excinfo:
            job.full_clean(exclude=["plan_occurrence"])

        assert "location" in excinfo.value.message_dict

    def test_a_consistent_job_saves(self, organization):
        job = JobFactory(organization=organization)

        assert job.pk is not None
        assert job.location.customer_id == job.customer_id


@pytest.mark.django_db
class TestJobConstraints:
    def test_a_job_must_end_after_it_starts(self, organization):
        start = timezone.now()

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                JobFactory(
                    organization=organization,
                    scheduled_start=start,
                    scheduled_end=start - dt.timedelta(hours=1),
                )

    def test_a_zero_length_job_is_rejected(self, organization):
        start = timezone.now()

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                JobFactory(organization=organization, scheduled_start=start, scheduled_end=start)

    def test_one_job_per_plan_occurrence(self, organization):
        """
        The materializer runs daily and leans on this to stay idempotent.
        Without it, every run creates another copy of the same visit.
        """
        plan = RecurringPlanFactory(organization=organization)
        occurrence = timezone.now() + dt.timedelta(days=3)

        JobFactory(
            organization=organization,
            customer=plan.customer,
            location=plan.location,
            service=plan.service,
            plan=plan,
            plan_occurrence=occurrence,
            scheduled_start=occurrence,
            scheduled_end=occurrence + dt.timedelta(hours=2),
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                JobFactory(
                    organization=organization,
                    customer=plan.customer,
                    location=plan.location,
                    service=plan.service,
                    plan=plan,
                    plan_occurrence=occurrence,
                    scheduled_start=occurrence,
                    scheduled_end=occurrence + dt.timedelta(hours=2),
                )

    def test_the_occurrence_constraint_ignores_soft_deleted_rows(self, organization):
        """Regeneration soft-deletes then re-creates; the constraint must allow that."""
        plan = RecurringPlanFactory(organization=organization)
        occurrence = timezone.now() + dt.timedelta(days=3)
        common = {
            "organization": organization,
            "customer": plan.customer,
            "location": plan.location,
            "service": plan.service,
            "plan": plan,
            "plan_occurrence": occurrence,
            "scheduled_start": occurrence,
            "scheduled_end": occurrence + dt.timedelta(hours=2),
        }

        first = JobFactory(**common)
        first.delete()

        second = JobFactory(**common)

        assert second.pk != first.pk
        assert Job.objects.filter(plan=plan).count() == 1

    def test_jobs_without_a_plan_are_not_constrained_to_one_occurrence(self, organization):
        """A one-off job has a null plan; several may share a start time."""
        start = timezone.now() + dt.timedelta(days=1)

        JobFactory(organization=organization, scheduled_start=start)
        JobFactory(organization=organization, scheduled_start=start)

        assert Job.objects.filter(scheduled_start=start).count() == 2


@pytest.mark.django_db
class TestTimeEntryConstraints:
    def test_only_one_open_entry_per_job_and_user(self, organization):
        entry = TimeEntryFactory(organization=organization)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TimeEntry.objects.create(
                    organization=organization,
                    job=entry.job,
                    user=entry.user,
                    clock_in=timezone.now(),
                )

    def test_a_second_entry_is_allowed_once_the_first_is_closed(self, organization):
        """A cleaner can leave and come back; that is two closed stretches."""
        entry = TimeEntryFactory(organization=organization)
        entry.clock_out = entry.clock_in + dt.timedelta(hours=1)
        entry.save()

        second = TimeEntry.objects.create(
            organization=organization,
            job=entry.job,
            user=entry.user,
            clock_in=timezone.now(),
        )

        assert second.pk is not None

    def test_two_people_may_be_clocked_into_the_same_job(self, organization):
        entry = TimeEntryFactory(organization=organization)

        second = TimeEntry.objects.create(
            organization=organization,
            job=entry.job,
            user=UserFactory(),
            clock_in=timezone.now(),
        )

        assert second.pk is not None

    def test_clock_out_must_follow_clock_in(self, organization):
        now = timezone.now()

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TimeEntryFactory(
                    organization=organization,
                    clock_in=now,
                    clock_out=now - dt.timedelta(minutes=1),
                )

    def test_duration_is_none_while_open(self, organization):
        entry = TimeEntryFactory(organization=organization)

        assert entry.is_open
        assert entry.duration_minutes is None

    def test_duration_is_whole_minutes_once_closed(self, organization):
        entry = TimeEntryFactory(organization=organization)
        entry.clock_out = entry.clock_in + dt.timedelta(minutes=95, seconds=30)
        entry.save()

        assert entry.duration_minutes == 95


@pytest.mark.django_db
class TestRecurringPlan:
    def test_duration_defaults_from_the_service(self, organization):
        plan = RecurringPlanFactory.build(organization=organization, duration_minutes=0)
        plan.service = RecurringPlanFactory.service.get_factory()(
            organization=organization, default_duration_minutes=180
        )
        plan.customer = CustomerFactory(organization=organization)
        plan.location = ServiceLocationFactory(organization=organization, customer=plan.customer)
        plan.save()

        assert plan.duration_minutes == 180

    def test_a_location_belonging_to_another_customer_is_rejected(self, organization):
        plan = RecurringPlanFactory(organization=organization)
        plan.location = ServiceLocationFactory(organization=organization)

        with pytest.raises(ValidationError) as excinfo:
            plan.clean()

        assert "location" in excinfo.value.message_dict

    def test_ends_on_cannot_precede_starts_on(self, organization):
        plan = RecurringPlanFactory(organization=organization)
        plan.ends_on = plan.starts_on - dt.timedelta(days=1)

        with pytest.raises(ValidationError) as excinfo:
            plan.clean()

        assert "ends_on" in excinfo.value.message_dict

    def test_ends_on_equal_to_starts_on_is_allowed(self, organization):
        plan = RecurringPlanFactory(organization=organization)
        plan.ends_on = plan.starts_on

        plan.clean()  # does not raise


class TestValidateRrule:
    @pytest.mark.parametrize(
        "rule",
        [
            "FREQ=WEEKLY;BYDAY=TU",
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH",
            "FREQ=MONTHLY;BYMONTHDAY=1",
            "FREQ=DAILY;COUNT=10",
            "FREQ=WEEKLY;BYDAY=MO;UNTIL=20271231T000000",
            "FREQ=WEEKLY;BYDAY=MO;UNTIL=20271231",
        ],
    )
    def test_valid_rules_pass(self, rule):
        assert validate_rrule(rule) is None

    def test_a_utc_until_is_rejected_in_favour_of_ends_on(self):
        """
        Expansion is naive local wall-clock so that 9am stays 9am across a DST
        change. A UTC end instant cannot be mixed with that, and `ends_on`
        already expresses "stop after this local date".
        """
        message = validate_rrule("FREQ=WEEKLY;BYDAY=MO;UNTIL=20271231T000000Z")

        assert message is not None
        assert "ends_on" in message

    def test_an_empty_rule_is_rejected(self):
        assert validate_rrule("") is not None
        assert validate_rrule("   ") is not None

    def test_nonsense_is_rejected(self):
        assert validate_rrule("EVERY OTHER TUESDAY") is not None

    @pytest.mark.parametrize(
        "rule",
        [
            "DTSTART:20270302T090000Z\nFREQ=WEEKLY;BYDAY=TU",
            "dtstart:20270302T090000Z\nFREQ=WEEKLY",
        ],
    )
    def test_dtstart_is_rejected_outright(self, rule):
        """
        Not merely ignored. starts_on + preferred_start_time are the single
        source of the start instant; a DTSTART in the rule is a second one.
        """
        message = validate_rrule(rule)

        assert message is not None
        assert "DTSTART" in message

    def test_count_is_allowed_because_it_bounds_rather_than_starts(self):
        assert validate_rrule("FREQ=WEEKLY;BYDAY=TU;COUNT=5") is None


class TestJobStatusEnum:
    def test_terminal_statuses_are_the_three_endings(self):
        assert set(TERMINAL_STATUSES) == {
            JobStatus.COMPLETE,
            JobStatus.CANCELLED,
            JobStatus.NO_ACCESS,
        }

    def test_every_status_has_a_transition_entry(self):
        assert set(ALLOWED_TRANSITIONS) == set(JobStatus.values)

    def test_every_terminal_status_can_only_be_reopened(self):
        for status in TERMINAL_STATUSES:
            assert ALLOWED_TRANSITIONS[status] == (JobStatus.SCHEDULED,)

    def test_in_progress_cannot_be_cancelled(self):
        """Work that started is completed or recorded as no-access, not cancelled."""
        assert JobStatus.CANCELLED not in ALLOWED_TRANSITIONS[JobStatus.IN_PROGRESS]
