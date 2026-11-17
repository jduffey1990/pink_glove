"""
Booking a one-off visit: a job with no plan behind it.

`test_jobs_api.py` covers the shape of a create. This file is about who may
book, what a booking may point at, and booking a visit together with its crew.
The tenant questions are asked from both sides: nothing of another
organization's can be named in a booking, and nothing is left behind when one
is refused.
"""

import datetime as dt
import uuid

import pytest
from django.urls import reverse
from django.utils import timezone

from scheduling.models import Job, JobAssignment
from scheduling.tests.factories import CustomerFactory, ServiceFactory, ServiceLocationFactory
from users.enums import Role

LIST = reverse("scheduling:job-list")


def detail(job):
    return reverse("scheduling:job-detail", args=[job.id])


@pytest.fixture
def payload(customer, location, service):
    start = timezone.now().replace(second=0, microsecond=0) + dt.timedelta(days=3)
    return {
        "customer": str(customer.id),
        "location": str(location.id),
        "service": str(service.id),
        "scheduled_start": start.isoformat(),
        "scheduled_end": (start + dt.timedelta(hours=2)).isoformat(),
    }


@pytest.mark.django_db
class TestWhoMayBook:
    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.DISPATCHER])
    def test_every_dispatcher_tier_can(self, organization, make_member, make_client, payload, role):
        client = make_client(make_member(organization, role=role, email=f"{role}@example.com"))

        assert client.post(LIST, payload, format="json").status_code == 201

    def test_a_cleaner_cannot(self, cleaner_client, payload):
        assert cleaner_client.post(LIST, payload, format="json").status_code == 403
        assert not Job.objects.exists()

    def test_a_customer_cannot_book_their_own_visit(self, customer_client, payload):
        assert customer_client.post(LIST, payload, format="json").status_code == 403
        assert not Job.objects.exists()

    def test_an_anonymous_caller_cannot(self, api_client, payload):
        assert api_client.post(LIST, payload, format="json").status_code == 403


@pytest.mark.django_db
class TestWhatABookingBelongsTo:
    def test_the_organization_is_the_callers_whatever_the_payload_says(
        self, dispatcher_client, organization, other_organization, payload
    ):
        response = dispatcher_client.post(
            LIST, {**payload, "organization": str(other_organization.id)}, format="json"
        )

        job = Job.objects.get(id=response.json()["id"])
        assert response.status_code == 201
        assert job.organization_id == organization.id

    def test_it_has_no_plan_behind_it(self, dispatcher_client, payload):
        row = dispatcher_client.post(LIST, payload, format="json").json()

        assert row["plan"] is None
        assert row["plan_occurrence"] is None
        assert row["status"] == "scheduled"

    def test_a_plan_cannot_be_claimed_through_the_payload(
        self, dispatcher_client, organization, payload
    ):
        """`plan` and `plan_occurrence` are the materializer's, not a caller's."""
        from scheduling.tests.factories import RecurringPlanFactory

        plan = RecurringPlanFactory(organization=organization)

        row = dispatcher_client.post(
            LIST,
            {**payload, "plan": str(plan.id), "plan_occurrence": payload["scheduled_start"]},
            format="json",
        ).json()

        assert row["plan"] is None
        assert row["plan_occurrence"] is None

    def test_another_organizations_customer_is_refused(
        self, dispatcher_client, other_organization, payload
    ):
        rival = CustomerFactory(organization=other_organization)
        rival_location = ServiceLocationFactory(organization=other_organization, customer=rival)

        response = dispatcher_client.post(
            LIST,
            {**payload, "customer": str(rival.id), "location": str(rival_location.id)},
            format="json",
        )

        assert response.status_code == 400
        assert "customer" in response.json()
        assert "location" in response.json()
        assert not Job.objects.exists()

    def test_another_organizations_customer_with_our_location_is_refused(
        self, dispatcher_client, other_organization, payload
    ):
        rival = CustomerFactory(organization=other_organization)

        response = dispatcher_client.post(
            LIST, {**payload, "customer": str(rival.id)}, format="json"
        )

        assert response.status_code == 400
        assert not Job.objects.exists()

    def test_another_organizations_service_is_refused(
        self, dispatcher_client, other_organization, payload
    ):
        rival_service = ServiceFactory(organization=other_organization)

        response = dispatcher_client.post(
            LIST, {**payload, "service": str(rival_service.id)}, format="json"
        )

        assert response.status_code == 400
        assert "service" in response.json()
        assert not Job.objects.exists()

    def test_a_booking_is_invisible_to_the_other_organization(
        self, dispatcher_client, rival_owner, make_client, payload
    ):
        row = dispatcher_client.post(LIST, payload, format="json").json()
        rival_client = make_client(rival_owner)

        assert (
            rival_client.get(reverse("scheduling:job-detail", args=[row["id"]])).status_code == 404
        )
        assert rival_client.get(LIST).json()["count"] == 0


@pytest.mark.django_db
class TestBookingWithItsCrew:
    """
    `assignees` on create. One request, so a visit is never left on the board
    with half the crew the dispatcher asked for.
    """

    def test_the_crew_is_on_the_job_it_returns(
        self, dispatcher_client, dispatcher, cleaner, other_cleaner, payload
    ):
        response = dispatcher_client.post(
            LIST, {**payload, "assignees": [str(cleaner.id), str(other_cleaner.id)]}, format="json"
        )

        row = response.json()
        assert response.status_code == 201
        assert {a["user"] for a in row["assignments"]} == {str(cleaner.id), str(other_cleaner.id)}
        assert "assignees" not in row
        assert set(
            JobAssignment.objects.filter(job_id=row["id"]).values_list("assigned_by", flat=True)
        ) == {dispatcher.id}

    def test_the_assigned_cleaner_can_then_see_it(
        self, dispatcher_client, cleaner_client, cleaner, payload
    ):
        """The point of assigning at all: it turns up on their day."""
        row = dispatcher_client.post(
            LIST, {**payload, "assignees": [str(cleaner.id)]}, format="json"
        ).json()

        mine = cleaner_client.get(LIST, {"mine": "true"}).json()["results"]
        assert [job["id"] for job in mine] == [row["id"]]

    def test_nobody_is_fine(self, dispatcher_client, payload):
        row = dispatcher_client.post(LIST, {**payload, "assignees": []}, format="json").json()

        assert row["assignments"] == []

    def test_naming_somebody_twice_is_harmless(self, dispatcher_client, cleaner, payload):
        row = dispatcher_client.post(
            LIST, {**payload, "assignees": [str(cleaner.id), str(cleaner.id)]}, format="json"
        ).json()

        assert len(row["assignments"]) == 1

    def test_another_organizations_cleaner_is_refused_and_nothing_is_booked(
        self, dispatcher_client, cleaner, rival_cleaner, payload
    ):
        """All or nothing: the good assignee does not leave a job behind either."""
        response = dispatcher_client.post(
            LIST,
            {**payload, "assignees": [str(cleaner.id), str(rival_cleaner.id)]},
            format="json",
        )

        assert response.status_code == 400
        assert "assignees" in response.json()
        assert not Job.all_objects.exists()
        assert not JobAssignment.all_objects.exists()

    def test_a_customer_cannot_be_put_on_a_job(self, dispatcher_client, customer_user, payload):
        response = dispatcher_client.post(
            LIST, {**payload, "assignees": [str(customer_user.id)]}, format="json"
        )

        assert response.status_code == 400
        assert "assignees" in response.json()
        assert not Job.all_objects.exists()

    def test_an_unknown_id_reads_the_same_as_a_strangers(
        self, dispatcher_client, rival_cleaner, payload
    ):
        """Otherwise the two messages say which user ids exist elsewhere."""
        unknown = dispatcher_client.post(
            LIST, {**payload, "assignees": [str(uuid.uuid4())]}, format="json"
        )
        stranger = dispatcher_client.post(
            LIST, {**payload, "assignees": [str(rival_cleaner.id)]}, format="json"
        )

        assert unknown.status_code == stranger.status_code == 400
        assert unknown.json() == stranger.json()

    def test_something_that_is_not_an_id_is_a_400(self, dispatcher_client, payload):
        response = dispatcher_client.post(
            LIST, {**payload, "assignees": ["not-a-uuid"]}, format="json"
        )

        assert response.status_code == 400
        assert "assignees" in response.json()

    def test_it_is_refused_on_an_existing_job(self, dispatcher_client, job, cleaner):
        """
        Silently ignoring it would read as "saved". The crew of an existing
        job changes through assign and unassign, which carry their own rules.
        """
        response = dispatcher_client.patch(
            detail(job), {"assignees": [str(cleaner.id)]}, format="json"
        )

        assert response.status_code == 400
        assert "assign" in response.json()["assignees"][0]
        assert not JobAssignment.objects.filter(job=job).exists()
