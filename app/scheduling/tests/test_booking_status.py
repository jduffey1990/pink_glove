"""
What booking work does to a customer's status (ADR-037).

Booking is a new visit, a new active plan, a paused plan made active, and a
visit or active plan handed to another customer, all through the API. Lead and
Inactive become Active; Active stays; Archived is refused with a 400 and
nothing is written. The move is in the booking's transaction, so every
refused booking -- including the ones refused after a row was already
written -- changes no status.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from catalog.enums import PricingModel
from customers.enums import CustomerStatus
from customers.models import Customer
from scheduling.models import Job, RecurringPlan
from scheduling.services import ARCHIVED_CUSTOMER_REFUSAL, materialize_plan
from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    RecurringPlanFactory,
    ServiceFactory,
    ServiceLocationFactory,
)
from users.enums import Role

JOBS = reverse("scheduling:job-list")
PLANS = reverse("scheduling:plan-list")


def plan_detail(plan) -> str:
    return reverse("scheduling:plan-detail", args=[plan.id])


def job_detail(job) -> str:
    return reverse("scheduling:job-detail", args=[job.id])


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


def set_status(customer, status) -> None:
    customer.status = status
    customer.save(update_fields=["status"])


def book_visit(client, organization, target, target_location, service):
    start = timezone.now().replace(second=0, microsecond=0) + dt.timedelta(days=3)
    return client.post(
        JOBS,
        {
            "customer": str(target.id),
            "location": str(target_location.id),
            "service": str(service.id),
            "scheduled_start": start.isoformat(),
            "scheduled_end": (start + dt.timedelta(hours=2)).isoformat(),
        },
        format="json",
    )


def book_plan(client, organization, target, target_location, service):
    return client.post(
        PLANS,
        {
            "customer": str(target.id),
            "location": str(target_location.id),
            "service": str(service.id),
            "rrule": "FREQ=WEEKLY;BYDAY=TU",
            "starts_on": timezone.now().date().isoformat(),
            "preferred_start_time": "09:00:00",
            "duration_minutes": 120,
        },
        format="json",
    )


def reactivate_plan(client, organization, target, target_location, service):
    plan = RecurringPlanFactory(
        organization=organization,
        customer=target,
        location=target_location,
        service=service,
        is_active=False,
    )
    return client.patch(plan_detail(plan), {"is_active": True}, format="json")


def hand_over_plan(client, organization, target, target_location, service):
    plan = RecurringPlanFactory(organization=organization, service=service)
    body = {"customer": str(target.id), "location": str(target_location.id)}
    return client.patch(plan_detail(plan), body, format="json")


def hand_over_visit(client, organization, target, target_location, service):
    job = JobFactory(organization=organization, service=service)
    body = {"customer": str(target.id), "location": str(target_location.id)}
    return client.patch(job_detail(job), body, format="json")


TRIGGERS = [book_visit, book_plan, reactivate_plan, hand_over_plan, hand_over_visit]


@pytest.mark.django_db
class TestEveryTriggerAndEveryStatus:
    """Each booking path against each status, on each side of each line."""

    @pytest.fixture
    def target(self, organization):
        customer = CustomerFactory(organization=organization)
        return customer, ServiceLocationFactory(organization=organization, customer=customer)

    @pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda t: t.__name__)
    @pytest.mark.parametrize("status", [CustomerStatus.LEAD, CustomerStatus.INACTIVE])
    def test_lead_and_inactive_become_active(
        self, authed_client, organization, service, target, trigger, status
    ):
        customer, location = target
        set_status(customer, status)

        response = trigger(authed_client, organization, customer, location, service)

        assert response.status_code in (200, 201)
        assert status_of(customer) == CustomerStatus.ACTIVE

    @pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda t: t.__name__)
    def test_active_stays_active_and_is_not_written(
        self, authed_client, organization, service, target, trigger
    ):
        customer, location = target
        set_status(customer, CustomerStatus.ACTIVE)
        before = Customer.objects.get(pk=customer.pk).updated_at

        response = trigger(authed_client, organization, customer, location, service)

        assert response.status_code in (200, 201)
        assert status_of(customer) == CustomerStatus.ACTIVE
        assert customer.updated_at == before

    @pytest.mark.parametrize("trigger", TRIGGERS, ids=lambda t: t.__name__)
    def test_archived_is_refused_and_nothing_is_written(
        self, authed_client, organization, service, target, trigger
    ):
        customer, location = target
        set_status(customer, CustomerStatus.ARCHIVED)
        plans_before = set(RecurringPlan.objects.values_list("id", "customer_id", "is_active"))
        jobs_before = set(Job.objects.values_list("id", "customer_id"))

        response = trigger(authed_client, organization, customer, location, service)

        assert response.status_code == 400
        assert response.json() == {"customer": [ARCHIVED_CUSTOMER_REFUSAL]}
        assert status_of(customer) == CustomerStatus.ARCHIVED
        # The reactivate and hand-over triggers make their row before the
        # request; whatever existed then is exactly what exists now.
        plans_after = set(RecurringPlan.objects.values_list("id", "customer_id", "is_active"))
        assert plans_after == plans_before or trigger in (reactivate_plan, hand_over_plan)
        assert not RecurringPlan.objects.filter(customer=customer, is_active=True).exists()
        assert not Job.objects.filter(customer=customer).exists()
        assert set(Job.objects.values_list("id", "customer_id")) >= jobs_before

    def test_a_paused_plan_books_nothing_and_moves_no_one(self, authed_client, customer, plan):
        response = authed_client.post(PLANS, {**plan, "is_active": False}, format="json")

        assert response.status_code == 201
        assert status_of(customer) == CustomerStatus.LEAD


@pytest.mark.django_db
class TestAnArchivedCustomersExistingWork:
    @pytest.fixture
    def archived(self, customer):
        set_status(customer, CustomerStatus.ARCHIVED)
        return customer

    def test_a_paused_plan_may_be_created_for_them(self, authed_client, archived, plan):
        """It books nothing; making it active is what is refused."""
        response = authed_client.post(PLANS, {**plan, "is_active": False}, format="json")

        assert response.status_code == 201
        assert status_of(archived) == CustomerStatus.ARCHIVED

    def test_a_paused_plan_is_not_handed_to_them(
        self, authed_client, organization, archived, location
    ):
        plan = RecurringPlanFactory(organization=organization, is_active=False)

        response = authed_client.patch(
            plan_detail(plan),
            {"customer": str(archived.id), "location": str(location.id)},
            format="json",
        )

        assert response.status_code == 400
        assert response.json() == {"customer": [ARCHIVED_CUSTOMER_REFUSAL]}
        plan.refresh_from_db()
        assert plan.customer_id != archived.id

    def test_their_existing_visit_can_still_be_edited(
        self, authed_client, organization, location, service, customer
    ):
        job = JobFactory(
            organization=organization, customer=customer, location=location, service=service
        )
        set_status(customer, CustomerStatus.ARCHIVED)

        response = authed_client.patch(job_detail(job), {"notes": "Key under mat."}, format="json")

        assert response.status_code == 200
        assert status_of(customer) == CustomerStatus.ARCHIVED

    def test_their_active_plan_can_be_paused(self, authed_client, organization, customer):
        plan = RecurringPlanFactory(organization=organization, customer=customer)
        materialize_plan(plan)
        set_status(customer, CustomerStatus.ARCHIVED)

        response = authed_client.patch(plan_detail(plan), {"is_active": False}, format="json")

        assert response.status_code == 200
        assert status_of(customer) == CustomerStatus.ARCHIVED

    def test_their_active_plan_is_not_otherwise_edited(self, authed_client, organization, customer):
        """A regenerating edit would book them new visits: pause it first, or unarchive them."""
        plan = RecurringPlanFactory(organization=organization, customer=customer)
        materialize_plan(plan)
        set_status(customer, CustomerStatus.ARCHIVED)
        jobs_before = set(Job.objects.filter(plan=plan).values_list("id", flat=True))

        response = authed_client.patch(
            plan_detail(plan), {"preferred_start_time": "14:00:00"}, format="json"
        )

        assert response.status_code == 400
        assert response.json() == {"customer": [ARCHIVED_CUSTOMER_REFUSAL]}
        assert set(Job.objects.filter(plan=plan).values_list("id", flat=True)) == jobs_before

    def test_a_soft_deleted_customer_is_refused_as_before(self, authed_client, customer, visit):
        customer.delete()

        response = authed_client.post(JOBS, visit, format="json")

        assert response.status_code == 400
        assert "customer" in response.json()
        assert not Job.objects.exists()


@pytest.mark.django_db
class TestTheNightlyRunForAnArchivedCustomer:
    """
    An active plan whose customer was archived after it was booked keeps the
    visits already on the board and gets no new ones (ADR-037).
    """

    @pytest.fixture
    def booked_then_archived(self, organization, customer):
        plan = RecurringPlanFactory(organization=organization, customer=customer)
        today = timezone.now().date()
        materialize_plan(plan, today=today)
        set_status(customer, CustomerStatus.ARCHIVED)
        plan.refresh_from_db()
        return plan, today

    def test_existing_visits_are_kept_and_no_new_ones_made(self, booked_then_archived):
        plan, today = booked_then_archived
        before = set(Job.objects.filter(plan=plan).values_list("id", flat=True))
        assert before

        assert materialize_plan(plan, today=today + dt.timedelta(days=28)) == 0
        assert set(Job.objects.filter(plan=plan).values_list("id", flat=True)) == before

    def test_the_organization_task_skips_it_too(self, booked_then_archived, organization):
        from scheduling.tasks import materialize_organization

        plan, _ = booked_then_archived
        assert Job.objects.filter(plan=plan).exists()
        Job.objects.filter(plan=plan).delete()  # room for the run to refill, if it would

        assert materialize_organization(str(organization.id)) == 0
        assert not Job.objects.filter(plan=plan).exists()

    def test_unarchiving_lets_the_next_run_resume(self, booked_then_archived, customer):
        plan, today = booked_then_archived
        set_status(customer, CustomerStatus.INACTIVE)
        plan.refresh_from_db()

        assert materialize_plan(plan, today=today + dt.timedelta(days=28)) > 0
        assert status_of(customer) == CustomerStatus.INACTIVE


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


@pytest.fixture
def lead_elsewhere(organization):
    """A second Lead in the same organization, with a location to point work at."""
    lead = CustomerFactory(organization=organization)
    return lead, ServiceLocationFactory(organization=organization, customer=lead)


def repoint(lead_elsewhere) -> dict:
    lead, location = lead_elsewhere
    return {"customer": str(lead.id), "location": str(location.id)}


@pytest.mark.django_db
class TestAnEditThatBooks:
    def test_reactivating_a_paused_plan_promotes(self, authed_client, organization, customer):
        plan = RecurringPlanFactory(organization=organization, customer=customer, is_active=False)

        response = authed_client.patch(plan_detail(plan), {"is_active": True}, format="json")

        assert response.status_code == 200
        assert status_of(customer) == CustomerStatus.ACTIVE

    def test_editing_a_paused_plan_without_reactivating_does_not(
        self, authed_client, organization, customer
    ):
        plan = RecurringPlanFactory(organization=organization, customer=customer, is_active=False)

        response = authed_client.patch(plan_detail(plan), {"notes": "Side gate."}, format="json")

        assert response.status_code == 200
        assert status_of(customer) == CustomerStatus.LEAD

    def test_editing_an_active_plan_for_the_same_customer_does_not(
        self, authed_client, organization, customer
    ):
        """Somebody set them back to Lead by hand; an unrelated edit does not overrule that."""
        plan = RecurringPlanFactory(organization=organization, customer=customer)

        response = authed_client.patch(
            plan_detail(plan), {"preferred_start_time": "14:00:00"}, format="json"
        )

        assert response.status_code == 200
        assert status_of(customer) == CustomerStatus.LEAD

    def test_pausing_a_plan_does_not(self, authed_client, organization, customer):
        plan = RecurringPlanFactory(organization=organization, customer=customer)

        authed_client.patch(plan_detail(plan), {"is_active": False}, format="json")

        assert status_of(customer) == CustomerStatus.LEAD

    def test_handing_an_active_plan_to_another_customer_promotes_them(
        self, authed_client, organization, customer, lead_elsewhere
    ):
        plan = RecurringPlanFactory(organization=organization, customer=customer)

        response = authed_client.patch(plan_detail(plan), repoint(lead_elsewhere), format="json")

        assert response.status_code == 200
        assert status_of(lead_elsewhere[0]) == CustomerStatus.ACTIVE
        assert status_of(customer) == CustomerStatus.LEAD

    def test_the_old_customer_of_a_plan_is_never_demoted(
        self, authed_client, organization, customer, lead_elsewhere
    ):
        customer.status = CustomerStatus.ACTIVE
        customer.save(update_fields=["status"])
        plan = RecurringPlanFactory(organization=organization, customer=customer)

        authed_client.patch(plan_detail(plan), repoint(lead_elsewhere), format="json")

        assert status_of(customer) == CustomerStatus.ACTIVE

    def test_handing_a_paused_plan_to_another_customer_does_not(
        self, authed_client, organization, customer, lead_elsewhere
    ):
        plan = RecurringPlanFactory(organization=organization, customer=customer, is_active=False)

        response = authed_client.patch(plan_detail(plan), repoint(lead_elsewhere), format="json")

        assert response.status_code == 200
        assert status_of(lead_elsewhere[0]) == CustomerStatus.LEAD

    def test_handing_a_visit_to_another_customer_promotes_them(
        self, authed_client, organization, customer, location, service, lead_elsewhere
    ):
        customer.status = CustomerStatus.ACTIVE
        customer.save(update_fields=["status"])
        job = JobFactory(
            organization=organization, customer=customer, location=location, service=service
        )

        response = authed_client.patch(job_detail(job), repoint(lead_elsewhere), format="json")

        assert response.status_code == 200
        assert status_of(lead_elsewhere[0]) == CustomerStatus.ACTIVE
        assert status_of(customer) == CustomerStatus.ACTIVE

    def test_editing_a_visit_for_the_same_customer_does_not(
        self, authed_client, organization, customer, location, service
    ):
        job = JobFactory(
            organization=organization, customer=customer, location=location, service=service
        )

        response = authed_client.patch(job_detail(job), {"notes": "Bring a ladder."}, format="json")

        assert response.status_code == 200
        assert status_of(customer) == CustomerStatus.LEAD


@pytest.mark.django_db
class TestARefusedEditLeavesALead:
    def test_a_reactivation_whose_visits_cannot_be_priced(
        self, authed_client, organization, customer
    ):
        """Refused after the plan row was saved, while rebuilding its visits."""
        plan = RecurringPlanFactory(
            organization=organization,
            customer=customer,
            is_active=False,
            location=ServiceLocationFactory(
                organization=organization, customer=customer, square_feet=None
            ),
            service=ServiceFactory(
                organization=organization,
                pricing_model=PricingModel.PER_SQFT,
                per_sqft_rate_cents=Decimal("10.000"),
            ),
        )

        response = authed_client.patch(plan_detail(plan), {"is_active": True}, format="json")

        assert response.status_code == 400
        plan.refresh_from_db()
        assert plan.is_active is False
        assert status_of(customer) == CustomerStatus.LEAD

    def test_a_plan_handed_over_with_a_location_that_is_not_theirs(
        self, authed_client, organization, customer, location, lead_elsewhere
    ):
        plan = RecurringPlanFactory(organization=organization, customer=customer)
        lead, _ = lead_elsewhere

        response = authed_client.patch(
            plan_detail(plan),
            {"customer": str(lead.id), "location": str(location.id)},
            format="json",
        )

        assert response.status_code == 400
        assert status_of(lead) == CustomerStatus.LEAD

    def test_a_visit_handed_over_with_a_location_that_is_not_theirs(
        self, authed_client, organization, customer, location, service, lead_elsewhere
    ):
        job = JobFactory(
            organization=organization, customer=customer, location=location, service=service
        )
        lead, _ = lead_elsewhere

        response = authed_client.patch(job_detail(job), {"customer": str(lead.id)}, format="json")

        assert response.status_code == 400
        assert status_of(lead) == CustomerStatus.LEAD


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
        from scheduling.services import activate_booked_customer

        rival = CustomerFactory(organization=other_organization)
        rival.organization_id = organization.id

        assert activate_booked_customer(rival) is False
        rival.refresh_from_db()
        assert rival.status == CustomerStatus.LEAD

    def test_handing_work_to_another_organizations_lead_is_refused_and_leaves_it(
        self, authed_client, organization, customer, location, service, other_organization
    ):
        rival = CustomerFactory(organization=other_organization)
        rival_location = ServiceLocationFactory(organization=other_organization, customer=rival)
        body = {"customer": str(rival.id), "location": str(rival_location.id)}
        plan = RecurringPlanFactory(organization=organization, customer=customer)
        job = JobFactory(
            organization=organization, customer=customer, location=location, service=service
        )

        assert authed_client.patch(plan_detail(plan), body, format="json").status_code == 400
        assert authed_client.patch(job_detail(job), body, format="json").status_code == 400
        assert status_of(rival) == CustomerStatus.LEAD
