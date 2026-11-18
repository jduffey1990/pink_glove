"""
Booking work for a Lead makes them Active (ADR-037).

The two booking paths are a one-off visit created through the API and a
recurring plan created through the API. Only `lead` moves. The promotion is in
the booking's transaction, so every refused booking -- including the ones
refused after a row was already written -- leaves the Lead a Lead.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from catalog.enums import PricingModel
from customers.enums import CustomerStatus
from scheduling.models import Job, RecurringPlan
from scheduling.services import materialize_plan
from scheduling.tests.factories import (
    CustomerFactory,
    RecurringPlanFactory,
    ServiceFactory,
    ServiceLocationFactory,
)
from users.enums import Role

JOBS = reverse("scheduling:job-list")
PLANS = reverse("scheduling:plan-list")


def status_of(customer) -> str:
    customer.refresh_from_db()
    return customer.status


@pytest.fixture
def visit(customer, location, service):
    start = timezone.now().replace(second=0, microsecond=0) + dt.timedelta(days=3)
    return {
        "customer": str(customer.id),
        "location": str(location.id),
        "service": str(service.id),
        "scheduled_start": start.isoformat(),
        "scheduled_end": (start + dt.timedelta(hours=2)).isoformat(),
    }


@pytest.fixture
def plan(customer, location, service):
    return {
        "customer": str(customer.id),
        "location": str(location.id),
        "service": str(service.id),
        "rrule": "FREQ=WEEKLY;BYDAY=TU",
        "starts_on": timezone.now().date().isoformat(),
        "preferred_start_time": "09:00:00",
        "duration_minutes": 120,
    }


@pytest.fixture
def unpriceable(organization, customer):
    """A location and service that cannot be quoted: per square foot, no square feet."""
    return {
        "location": str(
            ServiceLocationFactory(
                organization=organization, customer=customer, square_feet=None
            ).id
        ),
        "service": str(
            ServiceFactory(
                organization=organization,
                pricing_model=PricingModel.PER_SQFT,
                per_sqft_rate_cents=Decimal("10.000"),
            ).id
        ),
    }


@pytest.mark.django_db
class TestALeadIsPromoted:
    def test_by_a_one_off_visit(self, authed_client, customer, visit):
        assert customer.status == CustomerStatus.LEAD

        response = authed_client.post(JOBS, visit, format="json")

        assert response.status_code == 201
        assert status_of(customer) == CustomerStatus.ACTIVE

    def test_by_a_recurring_plan(self, authed_client, customer, plan):
        response = authed_client.post(PLANS, plan, format="json")

        assert response.status_code == 201
        assert Job.objects.filter(plan_id=response.json()["id"]).exists()
        assert status_of(customer) == CustomerStatus.ACTIVE

    def test_by_a_plan_whose_visits_fall_past_the_horizon(self, authed_client, customer, plan):
        """A standing booking that has not reached the board yet is still a booking."""
        starts_on = timezone.now().date() + dt.timedelta(days=200)

        response = authed_client.post(
            PLANS, {**plan, "starts_on": starts_on.isoformat()}, format="json"
        )

        assert response.status_code == 201
        assert not Job.objects.filter(plan_id=response.json()["id"]).exists()
        assert status_of(customer) == CustomerStatus.ACTIVE

    def test_by_every_role_that_may_book(self, organization, make_member, make_client, visit):
        for role in (Role.OWNER, Role.ADMIN, Role.DISPATCHER):
            lead = CustomerFactory(organization=organization)
            location = ServiceLocationFactory(organization=organization, customer=lead)
            client = make_client(make_member(organization, role=role, email=f"{role}@example.com"))

            response = client.post(
                JOBS,
                {**visit, "customer": str(lead.id), "location": str(location.id)},
                format="json",
            )

            assert response.status_code == 201
            assert status_of(lead) == CustomerStatus.ACTIVE

    def test_only_the_status_is_written(self, authed_client, customer, visit):
        """A narrow update: the customer's other fields are not saved back from a stale copy."""
        before = customer.updated_at

        authed_client.post(JOBS, visit, format="json")

        customer.refresh_from_db()
        assert customer.updated_at > before
        assert customer.first_name == "Dana"


@pytest.mark.django_db
class TestOnlyALeadMoves:
    @pytest.mark.parametrize(
        "status", [CustomerStatus.ACTIVE, CustomerStatus.INACTIVE, CustomerStatus.ARCHIVED]
    )
    def test_a_one_off_visit_leaves_it(self, authed_client, customer, visit, status):
        customer.status = status
        customer.save(update_fields=["status"])

        assert authed_client.post(JOBS, visit, format="json").status_code == 201
        assert status_of(customer) == status

    @pytest.mark.parametrize(
        "status", [CustomerStatus.ACTIVE, CustomerStatus.INACTIVE, CustomerStatus.ARCHIVED]
    )
    def test_a_plan_leaves_it(self, authed_client, customer, plan, status):
        customer.status = status
        customer.save(update_fields=["status"])

        assert authed_client.post(PLANS, plan, format="json").status_code == 201
        assert status_of(customer) == status

    def test_a_paused_plan_books_nothing_and_promotes_no_one(self, authed_client, customer, plan):
        response = authed_client.post(PLANS, {**plan, "is_active": False}, format="json")

        assert response.status_code == 201
        assert status_of(customer) == CustomerStatus.LEAD


@pytest.mark.django_db
class TestARefusedBookingLeavesALead:
    def test_a_location_of_another_customer(self, authed_client, organization, customer, visit):
        elsewhere = ServiceLocationFactory(organization=organization)

        response = authed_client.post(JOBS, {**visit, "location": str(elsewhere.id)}, format="json")

        assert response.status_code == 400
        assert status_of(customer) == CustomerStatus.LEAD

    def test_a_visit_that_ends_before_it_starts(self, authed_client, customer, visit):
        response = authed_client.post(
            JOBS, {**visit, "scheduled_end": visit["scheduled_start"]}, format="json"
        )

        assert response.status_code == 400
        assert status_of(customer) == CustomerStatus.LEAD

    def test_a_visit_that_cannot_be_priced(self, authed_client, customer, visit, unpriceable):
        response = authed_client.post(JOBS, {**visit, **unpriceable}, format="json")

        assert response.status_code == 400
        assert status_of(customer) == CustomerStatus.LEAD

    def test_a_crew_refused_after_the_visit_was_written(
        self, authed_client, customer, visit, rival_cleaner
    ):
        """The job row exists when the crew is refused; the transaction takes both back."""
        response = authed_client.post(
            JOBS, {**visit, "assignees": [str(rival_cleaner.id)]}, format="json"
        )

        assert response.status_code == 400
        assert "assignees" in response.json()
        assert not Job.objects.exists()
        assert status_of(customer) == CustomerStatus.LEAD

    def test_a_plan_with_a_location_of_another_customer(
        self, authed_client, organization, customer, plan
    ):
        elsewhere = ServiceLocationFactory(organization=organization)

        response = authed_client.post(PLANS, {**plan, "location": str(elsewhere.id)}, format="json")

        assert response.status_code == 400
        assert status_of(customer) == CustomerStatus.LEAD

    def test_a_plan_that_ends_before_it_starts(self, authed_client, customer, plan):
        ends_on = timezone.now().date() - dt.timedelta(days=1)

        response = authed_client.post(
            PLANS, {**plan, "ends_on": ends_on.isoformat()}, format="json"
        )

        assert response.status_code == 400
        assert status_of(customer) == CustomerStatus.LEAD

    def test_a_plan_whose_visits_cannot_be_priced(self, authed_client, customer, plan, unpriceable):
        """
        Refused after the plan row was written, while materializing its visits.
        The plan goes too: a 400 leaves nothing behind.
        """
        response = authed_client.post(PLANS, {**plan, **unpriceable}, format="json")

        assert response.status_code == 400
        assert "service" in response.json()
        assert not RecurringPlan.objects.exists()
        assert not Job.objects.exists()
        assert status_of(customer) == CustomerStatus.LEAD


@pytest.mark.django_db
class TestNothingElsePromotesOrDemotes:
    def test_deleting_the_visit_does_not_demote(self, authed_client, customer, visit):
        job_id = authed_client.post(JOBS, visit, format="json").json()["id"]

        response = authed_client.delete(reverse("scheduling:job-detail", args=[job_id]))

        assert response.status_code == 204
        assert status_of(customer) == CustomerStatus.ACTIVE

    def test_cancelling_the_visit_does_not_demote(self, authed_client, customer, visit):
        job_id = authed_client.post(JOBS, visit, format="json").json()["id"]

        response = authed_client.post(
            reverse("scheduling:job-status", args=[job_id]),
            {"status": "cancelled", "reason": "Changed their mind."},
            format="json",
        )

        assert response.status_code == 200
        assert status_of(customer) == CustomerStatus.ACTIVE

    def test_the_nightly_materialization_does_not_promote(self, organization, customer):
        """Only creating the plan through the API is a booking; topping up its visits is not."""
        plan = RecurringPlanFactory(
            organization=organization, customer=customer, starts_on=timezone.now().date()
        )

        assert materialize_plan(plan) > 0
        assert status_of(customer) == CustomerStatus.LEAD


@pytest.mark.django_db
class TestTenancy:
    def test_naming_another_organizations_lead_is_refused_and_leaves_it(
        self, authed_client, other_organization, visit, plan
    ):
        rival = CustomerFactory(organization=other_organization)
        ServiceLocationFactory(organization=other_organization, customer=rival)

        assert (
            authed_client.post(JOBS, {**visit, "customer": str(rival.id)}, format="json")
        ).status_code == 400
        assert (
            authed_client.post(PLANS, {**plan, "customer": str(rival.id)}, format="json")
        ).status_code == 400
        assert status_of(rival) == CustomerStatus.LEAD

    def test_a_booking_here_touches_no_customer_there(
        self, authed_client, customer, other_organization, visit, plan
    ):
        rival = CustomerFactory(organization=other_organization)
        rival_updated_at = rival.updated_at

        assert authed_client.post(JOBS, visit, format="json").status_code == 201
        assert authed_client.post(PLANS, plan, format="json").status_code == 201

        rival.refresh_from_db()
        assert status_of(customer) == CustomerStatus.ACTIVE
        assert rival.status == CustomerStatus.LEAD
        assert rival.updated_at == rival_updated_at

    def test_the_service_will_not_cross_organizations(self, other_organization, organization):
        """A customer copy carrying the wrong organization matches no row."""
        from scheduling.services import promote_lead

        rival = CustomerFactory(organization=other_organization)
        rival.organization_id = organization.id

        assert promote_lead(rival) is False
        rival.refresh_from_db()
        assert rival.status == CustomerStatus.LEAD
