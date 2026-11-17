"""
Moving a visit, and who may.

The question with consequences is what happens to a visit a recurring plan
created once a dispatcher has moved it by hand. ADR-020's answer is that it is
kept: regeneration rebuilds only untouched visits, and the materializer keys
on the instant the occurrence was *planned* for, so the moved visit is neither
thrown away nor re-created at its old slot. These tests drive that through the
API a dispatcher actually uses, PATCH then the plan's own endpoints, rather
than by saving the model.

What a hand edit that leaves the start alone does (a price, a longer slot, a
note) is in `test_plan_stamps.py`.
"""

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from scheduling.models import Job, JobAssignment
from scheduling.services import materialize_plan
from scheduling.tests.factories import (
    JobFactory,
    RecurringPlanFactory,
    ServiceFactory,
    ServiceLocationFactory,
)

LIST = reverse("scheduling:job-list")


def detail(job):
    return reverse("scheduling:job-detail", args=[job.id])


def plan_url(plan, action=None):
    if action:
        return reverse(f"scheduling:plan-{action}", args=[plan.id])
    return reverse("scheduling:plan-detail", args=[plan.id])


def move(client, job, *, by):
    start = job.scheduled_start + by
    return client.patch(
        detail(job),
        {
            "scheduled_start": start.isoformat(),
            "scheduled_end": (start + (job.scheduled_end - job.scheduled_start)).isoformat(),
        },
        format="json",
    )


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
def future(plan):
    """The plan's coming visits, a week out so none is about to start."""
    cutoff = timezone.now() + dt.timedelta(days=7)
    return list(
        Job.objects.filter(plan=plan, scheduled_start__gt=cutoff).order_by("scheduled_start")
    )


@pytest.mark.django_db
class TestMovingAVisit:
    def test_a_dispatcher_can_move_a_visit(self, dispatcher_client, job):
        start = job.scheduled_start + dt.timedelta(days=2, hours=3)

        response = move(dispatcher_client, job, by=dt.timedelta(days=2, hours=3))

        job.refresh_from_db()
        assert response.status_code == 200
        assert job.scheduled_start == start
        assert job.scheduled_end == start + dt.timedelta(hours=2)

    def test_the_price_is_not_requoted_by_a_move(self, dispatcher_client, job):
        """`price_cents` is what was agreed. A longer slot is not a new quote."""
        job.price_cents = 4242
        job.save()

        response = dispatcher_client.patch(
            detail(job),
            {"scheduled_end": (job.scheduled_end + dt.timedelta(hours=3)).isoformat()},
            format="json",
        )

        assert response.json()["price_cents"] == 4242
        assert response.json()["duration_minutes"] == 300

    def test_a_start_moved_past_the_end_is_refused(self, dispatcher_client, job):
        before = job.scheduled_start

        response = dispatcher_client.patch(
            detail(job),
            {"scheduled_start": (job.scheduled_end + dt.timedelta(hours=1)).isoformat()},
            format="json",
        )

        job.refresh_from_db()
        assert response.status_code == 400
        assert "scheduled_end" in response.json()
        assert job.scheduled_start == before

    def test_the_crew_stays_on_a_moved_visit(self, dispatcher_client, assigned_job, cleaner):
        move(dispatcher_client, assigned_job, by=dt.timedelta(days=1))

        assert JobAssignment.objects.filter(job=assigned_job, user=cleaner).exists()


@pytest.mark.django_db
class TestWhoMayChangeAVisit:
    def test_an_assigned_cleaner_cannot_move_it(self, cleaner_client, assigned_job):
        before = assigned_job.scheduled_start

        response = move(cleaner_client, assigned_job, by=dt.timedelta(hours=1))

        assigned_job.refresh_from_db()
        assert response.status_code == 403
        assert assigned_job.scheduled_start == before

    def test_an_assigned_cleaner_cannot_replace_it(
        self, cleaner_client, assigned_job, customer, location, service
    ):
        payload = {
            "customer": str(customer.id),
            "location": str(location.id),
            "service": str(service.id),
            "scheduled_start": assigned_job.scheduled_start.isoformat(),
            "scheduled_end": assigned_job.scheduled_end.isoformat(),
            "notes": "rewritten",
        }

        assert cleaner_client.put(detail(assigned_job), payload, format="json").status_code == 403

    def test_a_customer_cannot_move_their_own_visit(self, customer_client, job):
        before = job.scheduled_start

        response = move(customer_client, job, by=dt.timedelta(hours=1))

        job.refresh_from_db()
        assert response.status_code == 403
        assert job.scheduled_start == before

    def test_a_customer_cannot_delete_their_own_visit(self, customer_client, job):
        assert customer_client.delete(detail(job)).status_code == 403
        assert Job.objects.filter(pk=job.pk).exists()

    def test_another_organizations_visit_cannot_be_moved(self, dispatcher_client, rival_job):
        before = rival_job.scheduled_start

        response = move(dispatcher_client, rival_job, by=dt.timedelta(hours=1))

        rival_job.refresh_from_db()
        assert response.status_code == 404
        assert rival_job.scheduled_start == before

    def test_another_organizations_visit_cannot_be_deleted(self, dispatcher_client, rival_job):
        assert dispatcher_client.delete(detail(rival_job)).status_code == 404
        assert Job.objects.filter(pk=rival_job.pk).exists()

    def test_a_visit_cannot_be_pointed_at_another_organizations_location(
        self, dispatcher_client, job, location, other_organization
    ):
        rival_location = ServiceLocationFactory(organization=other_organization)

        response = dispatcher_client.patch(
            detail(job), {"location": str(rival_location.id)}, format="json"
        )

        job.refresh_from_db()
        assert response.status_code == 400
        assert job.location_id == location.id

    def test_a_visit_cannot_be_pointed_at_another_organizations_service(
        self, dispatcher_client, job, service, other_organization
    ):
        rival_service = ServiceFactory(organization=other_organization)

        response = dispatcher_client.patch(
            detail(job), {"service": str(rival_service.id)}, format="json"
        )

        job.refresh_from_db()
        assert response.status_code == 400
        assert "service" in response.json()
        assert job.service_id == service.id


@pytest.mark.django_db
class TestAMovedPlanVisit:
    """ADR-020, through the API."""

    def test_it_stays_the_plans_and_remembers_where_it_was_planned(
        self, dispatcher_client, plan, future
    ):
        visit = future[0]
        planned_for = visit.plan_occurrence

        row = move(dispatcher_client, visit, by=dt.timedelta(hours=3)).json()

        visit.refresh_from_db()
        assert row["plan"] == str(plan.id)
        assert visit.plan_occurrence == planned_for
        assert visit.scheduled_start == planned_for + dt.timedelta(hours=3)

    def test_the_planned_instant_cannot_be_rewritten(self, dispatcher_client, future):
        """It is the materializer's idempotency key. A caller may not move it."""
        visit = future[0]
        planned_for = visit.plan_occurrence

        dispatcher_client.patch(
            detail(visit),
            {"plan_occurrence": (planned_for + dt.timedelta(days=1)).isoformat(), "plan": None},
            format="json",
        )

        visit.refresh_from_db()
        assert visit.plan_occurrence == planned_for
        assert visit.plan_id is not None

    def test_it_survives_regenerate(self, dispatcher_client, plan, future):
        visit = future[0]
        move(dispatcher_client, visit, by=dt.timedelta(hours=3))
        moved_to = Job.objects.get(pk=visit.pk).scheduled_start

        response = dispatcher_client.post(plan_url(plan, "regenerate"))

        visit.refresh_from_db()
        assert response.status_code == 200
        assert response.json()["kept"] == 1
        assert visit.deleted_at is None
        assert visit.scheduled_start == moved_to

    def test_it_survives_an_edit_to_the_plan(self, dispatcher_client, plan, future):
        """The common case: the plan moves to the afternoon, this visit stays put."""
        visit, neighbour = future[0], future[1]
        move(dispatcher_client, visit, by=dt.timedelta(hours=3))
        moved_to = Job.objects.get(pk=visit.pk).scheduled_start

        body = dispatcher_client.patch(
            plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json"
        ).json()

        visit.refresh_from_db()
        assert body["kept"] == 1
        assert visit.deleted_at is None
        assert visit.scheduled_start == moved_to
        # The untouched neighbour was rebuilt, so the test is not passing
        # because regeneration did nothing at all.
        assert not Job.objects.filter(pk=neighbour.pk).exists()

    def test_its_old_slot_is_not_refilled(self, dispatcher_client, plan, future):
        """
        Keyed on `scheduled_start`, tonight's run would put a second visit
        back where this one used to be and the customer would get two.
        """
        visit = future[0]
        move(dispatcher_client, visit, by=dt.timedelta(days=1, hours=3))

        dispatcher_client.post(plan_url(plan, "regenerate"))
        created_tonight = materialize_plan(plan)

        at_this_occurrence = Job.objects.filter(plan=plan, plan_occurrence=visit.plan_occurrence)
        assert created_tonight == 0
        assert [row.pk for row in at_this_occurrence] == [visit.pk]

    def test_moving_it_back_makes_it_the_plans_to_rebuild_again(
        self, dispatcher_client, plan, future
    ):
        """
        "Untouched" is derived from where the visit is, not from a flag that
        somebody once moved it (ADR-020). Back at its planned instant, it is
        an ordinary plan visit again.
        """
        visit = future[0]
        move(dispatcher_client, visit, by=dt.timedelta(hours=3))
        visit.refresh_from_db()
        move(dispatcher_client, visit, by=-dt.timedelta(hours=3))

        body = dispatcher_client.post(plan_url(plan, "regenerate")).json()

        assert body["kept"] == 0
        assert not Job.objects.filter(pk=visit.pk).exists()

    def test_the_regenerate_endpoint_is_a_404_for_another_organization(
        self, rival_owner, make_client, plan
    ):
        before = set(Job.objects.filter(plan=plan).values_list("pk", flat=True))

        response = make_client(rival_owner).post(plan_url(plan, "regenerate"))

        assert response.status_code == 404
        assert set(Job.objects.filter(plan=plan).values_list("pk", flat=True)) == before

    def test_a_cleaner_cannot_regenerate(self, cleaner_client, plan):
        assert cleaner_client.post(plan_url(plan, "regenerate")).status_code == 403


@pytest.mark.django_db
class TestDoubleBooking:
    """
    There is no rule, and this says so on purpose.

    The same cleaner can be put on two visits that overlap and nothing
    refuses or warns: not on create, not on assign, not on a move. Whether
    that should be a refusal, a warning or nothing is a product decision that
    has not been made; when it is, these are the tests that change.
    """

    @pytest.fixture
    def slot(self, customer, location, service):
        start = timezone.now().replace(second=0, microsecond=0) + dt.timedelta(days=3)
        return {
            "customer": str(customer.id),
            "location": str(location.id),
            "service": str(service.id),
            "scheduled_start": start.isoformat(),
            "scheduled_end": (start + dt.timedelta(hours=2)).isoformat(),
        }

    def test_one_cleaner_can_be_booked_onto_two_visits_at_the_same_time(
        self, dispatcher_client, cleaner, slot
    ):
        first = dispatcher_client.post(
            LIST, {**slot, "assignees": [str(cleaner.id)]}, format="json"
        )
        second = dispatcher_client.post(
            LIST, {**slot, "assignees": [str(cleaner.id)]}, format="json"
        )

        assert first.status_code == second.status_code == 201
        assert JobAssignment.objects.filter(user=cleaner).count() == 2

    def test_assigning_onto_an_overlap_is_accepted(
        self, dispatcher_client, organization, assigned_job, cleaner
    ):
        overlapping = JobFactory(
            organization=organization,
            scheduled_start=assigned_job.scheduled_start + dt.timedelta(minutes=30),
        )

        response = dispatcher_client.post(
            reverse("scheduling:job-assign", args=[overlapping.id]),
            {"user": str(cleaner.id)},
            format="json",
        )

        assert response.status_code == 200

    def test_moving_a_visit_onto_an_overlap_is_accepted(
        self, dispatcher_client, organization, assigned_job, cleaner
    ):
        elsewhere = JobFactory(
            organization=organization,
            scheduled_start=assigned_job.scheduled_start + dt.timedelta(days=5),
        )
        JobAssignment.objects.create(organization=organization, job=elsewhere, user=cleaner)

        response = move(dispatcher_client, elsewhere, by=-dt.timedelta(days=5))

        assert response.status_code == 200
