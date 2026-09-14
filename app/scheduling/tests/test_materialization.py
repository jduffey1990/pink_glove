"""
Materialization and regeneration.

The daily beat task runs `materialize_plan` for every active plan, so
idempotency is not a nicety here: a materializer that creates a duplicate on
the second run sends two crews to one house. `plan_occurrence` is what
prevents it, and `test_materializing_twice_creates_nothing_new` is the test
that would catch its loss.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from catalog.enums import PricingModel
from scheduling.enums import JobStatus
from scheduling.models import Job, JobAssignment, TimeEntry
from scheduling.services import (
    MATERIALIZATION_HORIZON,
    materialize_plan,
    organization_today,
    regenerate_plan,
)
from scheduling.tests.factories import (
    RecurringPlanFactory,
    ServiceFactory,
    ServiceLocationFactory,
    UserFactory,
)

TODAY = dt.date(2027, 6, 1)  # a Tuesday


@pytest.fixture
def plan(organization):
    return RecurringPlanFactory(
        organization=organization,
        rrule="FREQ=WEEKLY;BYDAY=TU",
        starts_on=TODAY,
        preferred_start_time=dt.time(9, 0),
        duration_minutes=120,
    )


@pytest.mark.django_db
class TestMaterializePlan:
    def test_it_creates_jobs_out_to_the_horizon(self, plan):
        created = materialize_plan(plan, today=TODAY)

        jobs = Job.objects.filter(plan=plan).order_by("scheduled_start")
        assert created == jobs.count()
        assert created == 9  # 8 weeks of Tuesdays, inclusive of today's

        last = jobs.last()
        assert last.scheduled_start.date() <= TODAY + MATERIALIZATION_HORIZON

    def test_materializing_twice_creates_nothing_new(self, plan):
        """The beat task runs daily. This is what keeps that from duplicating."""
        first = materialize_plan(plan, today=TODAY)
        second = materialize_plan(plan, today=TODAY)

        assert second == 0
        assert Job.objects.filter(plan=plan).count() == first

    def test_a_later_run_extends_the_horizon(self, plan):
        materialize_plan(plan, today=TODAY)
        before = Job.objects.filter(plan=plan).count()

        created = materialize_plan(plan, today=TODAY + dt.timedelta(days=7))

        assert created == 1
        assert Job.objects.filter(plan=plan).count() == before + 1

    def test_nothing_is_created_before_starts_on(self, plan):
        plan.starts_on = TODAY + dt.timedelta(days=14)
        plan.save()

        materialize_plan(plan, today=TODAY)

        earliest = Job.objects.filter(plan=plan).order_by("scheduled_start").first()
        assert earliest.scheduled_start.astimezone(plan.organization.tz).date() >= plan.starts_on

    def test_ends_on_caps_the_series(self, plan):
        plan.ends_on = TODAY + dt.timedelta(days=14)
        plan.save()

        created = materialize_plan(plan, today=TODAY)

        assert created == 3  # Jun 1, 8, 15

    def test_an_inactive_plan_materializes_nothing(self, plan):
        plan.is_active = False
        plan.save()

        assert materialize_plan(plan, today=TODAY) == 0
        assert not Job.objects.filter(plan=plan).exists()

    def test_a_plan_that_already_ended_materializes_nothing(self, plan):
        plan.ends_on = TODAY - dt.timedelta(days=1)
        plan.save()

        assert materialize_plan(plan, today=TODAY) == 0

    def test_each_job_spans_the_plans_duration(self, plan):
        materialize_plan(plan, today=TODAY)

        job = Job.objects.filter(plan=plan).first()

        assert job.scheduled_end - job.scheduled_start == dt.timedelta(minutes=120)

    def test_the_occurrence_is_recorded_alongside_the_start(self, plan):
        materialize_plan(plan, today=TODAY)

        job = Job.objects.filter(plan=plan).first()

        assert job.plan_occurrence == job.scheduled_start
        assert not job.was_rescheduled

    def test_plan_notes_are_copied_onto_each_job(self, plan):
        plan.notes = "Side gate is easiest."
        plan.save()

        materialize_plan(plan, today=TODAY)

        assert Job.objects.filter(plan=plan).first().notes == "Side gate is easiest."

    def test_jobs_start_out_scheduled(self, plan):
        materialize_plan(plan, today=TODAY)

        assert set(Job.objects.filter(plan=plan).values_list("status", flat=True)) == {
            JobStatus.SCHEDULED
        }


@pytest.mark.django_db
class TestMaterializedPricing:
    def test_the_service_quote_is_snapshotted(self, organization):
        service = ServiceFactory(
            organization=organization, pricing_model=PricingModel.FLAT, base_price_cents=12500
        )
        plan = RecurringPlanFactory(organization=organization, service=service, starts_on=TODAY)

        materialize_plan(plan, today=TODAY)

        assert Job.objects.filter(plan=plan).first().price_cents == 12500

    def test_raising_the_price_does_not_touch_existing_jobs(self, organization):
        """Last month's completed work keeps last month's price."""
        service = ServiceFactory(
            organization=organization, pricing_model=PricingModel.FLAT, base_price_cents=12500
        )
        plan = RecurringPlanFactory(organization=organization, service=service, starts_on=TODAY)
        materialize_plan(plan, today=TODAY)

        service.base_price_cents = 20000
        service.save()

        assert Job.objects.filter(plan=plan).first().price_cents == 12500

    def test_a_price_override_beats_the_quote(self, organization):
        service = ServiceFactory(
            organization=organization, pricing_model=PricingModel.FLAT, base_price_cents=12500
        )
        plan = RecurringPlanFactory(
            organization=organization,
            service=service,
            starts_on=TODAY,
            price_override_cents=9900,
        )

        materialize_plan(plan, today=TODAY)

        assert Job.objects.filter(plan=plan).first().price_cents == 9900

    def test_a_per_sqft_service_prices_against_the_location(self, organization):
        service = ServiceFactory(
            organization=organization,
            pricing_model=PricingModel.PER_SQFT,
            per_sqft_rate_cents=Decimal("10.000"),
            base_price_cents=0,
        )
        location = ServiceLocationFactory(organization=organization, square_feet=1500)
        plan = RecurringPlanFactory(
            organization=organization,
            service=service,
            customer=location.customer,
            location=location,
            starts_on=TODAY,
        )

        materialize_plan(plan, today=TODAY)

        assert Job.objects.filter(plan=plan).first().price_cents == 15000

    def test_a_missing_pricing_input_raises_rather_than_quoting_zero(self, organization):
        """Surfaced as a 400 by the API. Quoting zero silently would be worse."""
        service = ServiceFactory(
            organization=organization,
            pricing_model=PricingModel.PER_SQFT,
            per_sqft_rate_cents=Decimal("10.000"),
        )
        location = ServiceLocationFactory(organization=organization, square_feet=None)
        plan = RecurringPlanFactory(
            organization=organization,
            service=service,
            customer=location.customer,
            location=location,
            starts_on=TODAY,
        )

        with pytest.raises(ValueError, match="square_feet"):
            materialize_plan(plan, today=TODAY)


@pytest.mark.django_db
class TestDefaultAssignees:
    def test_the_standing_crew_is_assigned_to_every_new_job(self, organization):
        cleaner = UserFactory()
        plan = RecurringPlanFactory(
            organization=organization, starts_on=TODAY, default_assignees=[cleaner]
        )

        materialize_plan(plan, today=TODAY)

        jobs = Job.objects.filter(plan=plan)
        assert JobAssignment.objects.filter(job__in=jobs, user=cleaner).count() == jobs.count()

    def test_a_plan_with_no_default_crew_assigns_nobody(self, plan):
        materialize_plan(plan, today=TODAY)

        assert not JobAssignment.objects.filter(job__plan=plan).exists()

    def test_assignments_are_not_duplicated_on_a_second_run(self, organization):
        cleaner = UserFactory()
        plan = RecurringPlanFactory(
            organization=organization, starts_on=TODAY, default_assignees=[cleaner]
        )

        materialize_plan(plan, today=TODAY)
        materialize_plan(plan, today=TODAY)

        counts = [
            JobAssignment.objects.filter(job=job).count() for job in Job.objects.filter(plan=plan)
        ]
        assert set(counts) == {1}


@pytest.mark.django_db
class TestRegeneratePlan:
    """
    ADR-020: untouched future jobs are rebuilt, everything else is kept.

    These anchor to the organization's real local today rather than the fixed
    2027 date the pure materialization tests use. `regenerate_plan` splits the
    world on `timezone.now()`, so a plan parked entirely in 2027 would have no
    past at all and the "history is not rewritten" case could not be written.
    """

    @pytest.fixture
    def today(self, organization):
        return organization_today(organization)

    @pytest.fixture
    def spanning_plan(self, organization, today):
        """A weekly plan already four weeks old, so it has a past and a future."""
        plan = RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=WEEKLY;BYDAY=TU",
            starts_on=today - dt.timedelta(days=28),
            preferred_start_time=dt.time(9, 0),
            duration_minutes=120,
        )
        materialize_plan(plan, today=today - dt.timedelta(days=28))
        return plan

    @staticmethod
    def _future(plan):
        return Job.objects.filter(plan=plan, scheduled_start__gt=timezone.now())

    def test_untouched_future_jobs_are_replaced(self, spanning_plan, today):
        original_ids = set(self._future(spanning_plan).values_list("id", flat=True))
        assert original_ids

        spanning_plan.preferred_start_time = dt.time(14, 0)
        spanning_plan.save()
        result = regenerate_plan(spanning_plan, today=today)

        assert result["kept"] == 0
        assert result["regenerated"] > 0
        assert set(self._future(spanning_plan).values_list("id", flat=True)).isdisjoint(
            original_ids
        )

    def test_a_rescheduled_job_is_kept(self, spanning_plan, today):
        """
        The dispatcher moved this one at the customer's request. Regenerating
        it would throw that away, which is the whole point of ADR-020.
        """
        moved = self._future(spanning_plan).order_by("scheduled_start").last()
        moved.scheduled_start += dt.timedelta(days=1)
        moved.scheduled_end += dt.timedelta(days=1)
        moved.save()

        result = regenerate_plan(spanning_plan, today=today)

        moved.refresh_from_db()
        assert result["kept"] == 1
        assert moved.deleted_at is None
        assert moved.plan_id == spanning_plan.id

    def test_a_job_that_changed_status_is_kept(self, spanning_plan, today):
        started = self._future(spanning_plan).order_by("scheduled_start").last()
        started.status = JobStatus.EN_ROUTE
        started.save()

        result = regenerate_plan(spanning_plan, today=today)

        started.refresh_from_db()
        assert result["kept"] == 1
        assert started.deleted_at is None

    def test_a_job_with_a_time_entry_is_kept(self, spanning_plan, today, organization):
        worked = self._future(spanning_plan).order_by("scheduled_start").last()
        TimeEntry.objects.create(
            organization=organization,
            job=worked,
            user=UserFactory(),
            clock_in=timezone.now(),
        )

        result = regenerate_plan(spanning_plan, today=today)

        worked.refresh_from_db()
        assert result["kept"] == 1
        assert worked.deleted_at is None

    def test_past_jobs_are_never_touched(self, spanning_plan, today):
        """History is not rewritten by an edit to a forward-looking rule."""
        past_ids = set(
            Job.objects.filter(plan=spanning_plan, scheduled_start__lt=timezone.now()).values_list(
                "id", flat=True
            )
        )
        assert past_ids  # the fixture is only meaningful if some are in the past

        spanning_plan.preferred_start_time = dt.time(14, 0)
        spanning_plan.save()
        regenerate_plan(spanning_plan, today=today)

        assert Job.objects.filter(id__in=past_ids).count() == len(past_ids)

    def test_deactivating_drops_untouched_future_jobs_and_creates_none(self, spanning_plan, today):
        assert self._future(spanning_plan).exists()

        spanning_plan.is_active = False
        spanning_plan.save()
        result = regenerate_plan(spanning_plan, today=today)

        assert result["regenerated"] == 0
        assert not self._future(spanning_plan).exists()

    def test_deactivating_still_keeps_a_touched_job(self, spanning_plan, today):
        moved = self._future(spanning_plan).order_by("scheduled_start").last()
        moved.scheduled_start += dt.timedelta(days=1)
        moved.scheduled_end += dt.timedelta(days=1)
        moved.save()

        spanning_plan.is_active = False
        spanning_plan.save()
        result = regenerate_plan(spanning_plan, today=today)

        moved.refresh_from_db()
        assert result["kept"] == 1
        assert moved.deleted_at is None

    def test_regenerated_jobs_pick_up_the_new_definition(self, spanning_plan, today):
        spanning_plan.duration_minutes = 240
        spanning_plan.save()

        regenerate_plan(spanning_plan, today=today)

        job = self._future(spanning_plan).order_by("scheduled_start").first()
        assert job is not None
        assert job.scheduled_end - job.scheduled_start == dt.timedelta(minutes=240)

    def test_the_counts_add_up_to_the_future_jobs(self, spanning_plan, today):
        moved = self._future(spanning_plan).order_by("scheduled_start").last()
        moved.scheduled_start += dt.timedelta(days=1)
        moved.scheduled_end += dt.timedelta(days=1)
        moved.save()

        result = regenerate_plan(spanning_plan, today=today)

        assert result["regenerated"] + result["kept"] == self._future(spanning_plan).count()
