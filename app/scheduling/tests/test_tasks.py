"""
The nightly materializer.

Tasks run eagerly under the test settings, so `.delay()` inside the fan-out
executes inline and these assert on real rows rather than on mock calls.
"""

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from catalog.enums import PricingModel
from scheduling.models import Job
from scheduling.tasks import materialize_all_organizations, materialize_organization
from scheduling.tests.factories import (
    RecurringPlanFactory,
    ServiceFactory,
    ServiceLocationFactory,
)


@pytest.mark.django_db
class TestMaterializeOrganization:
    def test_it_fills_in_every_active_plan(self, organization):
        RecurringPlanFactory(organization=organization)
        RecurringPlanFactory(organization=organization, rrule="FREQ=WEEKLY;BYDAY=TH")

        created = materialize_organization(str(organization.id))

        assert created > 0
        assert Job.objects.filter(organization=organization).count() == created

    def test_inactive_plans_are_skipped(self, organization):
        RecurringPlanFactory(organization=organization, is_active=False)

        assert materialize_organization(str(organization.id)) == 0

    def test_running_it_twice_creates_nothing_new(self, organization):
        RecurringPlanFactory(organization=organization)

        first = materialize_organization(str(organization.id))
        second = materialize_organization(str(organization.id))

        assert first > 0
        assert second == 0

    def test_it_does_not_reach_into_another_organization(self, organization, other_organization):
        RecurringPlanFactory(organization=other_organization)

        assert materialize_organization(str(organization.id)) == 0
        assert not Job.objects.filter(organization=other_organization).exists()

    def test_an_unknown_organization_is_a_no_op(self):
        assert materialize_organization(str(uuid.uuid4())) == 0

    def test_an_inactive_organization_is_skipped(self, organization):
        RecurringPlanFactory(organization=organization)
        organization.is_active = False
        organization.save()

        assert materialize_organization(str(organization.id)) == 0

    def test_one_unpriceable_plan_does_not_cost_the_tenant_its_schedule(self, organization):
        """
        A per-sqft service on a location with no square footage raises. The
        other plans still have to run -- a nightly task that aborts on the
        first bad row leaves the whole tenant without a schedule.
        """
        broken_location = ServiceLocationFactory(organization=organization, square_feet=None)
        RecurringPlanFactory(
            organization=organization,
            customer=broken_location.customer,
            location=broken_location,
            service=ServiceFactory(
                organization=organization,
                pricing_model=PricingModel.PER_SQFT,
                per_sqft_rate_cents=Decimal("10.000"),
            ),
        )
        good_plan = RecurringPlanFactory(organization=organization)

        created = materialize_organization(str(organization.id))

        assert created > 0
        assert Job.objects.filter(plan=good_plan).count() == created


@pytest.mark.django_db
class TestMaterializeAllOrganizations:
    def test_every_active_organization_gets_a_task(self, organization, other_organization):
        RecurringPlanFactory(organization=organization)
        RecurringPlanFactory(organization=other_organization)

        queued = materialize_all_organizations()

        assert queued == 2
        assert Job.objects.filter(organization=organization).exists()
        assert Job.objects.filter(organization=other_organization).exists()

    def test_inactive_organizations_are_not_dispatched(self, organization, other_organization):
        other_organization.is_active = False
        other_organization.save()

        assert materialize_all_organizations() == 1

    def test_each_organization_materializes_in_its_own_timezone(
        self, organization, other_organization
    ):
        """Denver and New York both mean 9am locally, two hours apart in UTC."""
        common = {
            "rrule": "FREQ=WEEKLY;BYDAY=TU",
            "preferred_start_time": dt.time(9, 0),
        }
        denver_plan = RecurringPlanFactory(organization=organization, **common)
        ny_plan = RecurringPlanFactory(organization=other_organization, **common)

        materialize_all_organizations()

        denver_job = Job.objects.filter(plan=denver_plan).first()
        ny_job = Job.objects.filter(plan=ny_plan).first()

        assert denver_job.scheduled_start.astimezone(organization.tz).hour == 9
        assert ny_job.scheduled_start.astimezone(other_organization.tz).hour == 9
        assert denver_job.scheduled_start.hour != ny_job.scheduled_start.hour


@pytest.mark.django_db
class TestBeatSchedule:
    def test_both_periodic_tasks_are_registered(self, settings):
        """
        DatabaseScheduler syncs this dict on startup. A task that exists but is
        not in here simply never runs, silently.
        """
        tasks = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}

        assert "scheduling.materialize_all_organizations" in tasks
        assert "audit.evaluate_access_reveals_all" in tasks
