"""
Whether a visit may be edited or deleted, and that the server says so.

Two rules live here (ADR-032), both enforced by `JobViewSet` and published on
the job as `available_actions` from the same functions:

* a finished visit is not edited in place, it is reopened first;
* deleting is for a one-off visit nothing has happened to. Anything else is
  cancelled, because a deleted plan visit comes straight back (ADR-021).

The last class is the one that matters most to the frontend: what the job
publishes is exactly what the server then accepts, state by state.
"""

import datetime as dt

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from scheduling.enums import TERMINAL_STATUSES, JobStatus
from scheduling.models import Job
from scheduling.services import materialize_plan
from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    RecurringPlanFactory,
    ServiceLocationFactory,
    TimeEntryFactory,
)
from users.enums import Role

LIST = reverse("scheduling:job-list")

UNFINISHED = (JobStatus.SCHEDULED, JobStatus.EN_ROUTE, JobStatus.IN_PROGRESS)


def detail(job):
    return reverse("scheduling:job-detail", args=[job.id])


def status_url(job):
    return reverse("scheduling:job-status", args=[job.id])


def in_status(job, status):
    job.status = status
    job.save()
    return job


def move_payload(job, *, days=30):
    start = job.scheduled_start + dt.timedelta(days=days)
    return {
        "scheduled_start": start.isoformat(),
        "scheduled_end": (start + dt.timedelta(hours=1)).isoformat(),
    }


@pytest.fixture
def plan_job(organization, customer, location, service):
    """The next visit a live weekly plan put on the board."""
    plan = RecurringPlanFactory(
        organization=organization,
        customer=customer,
        location=location,
        service=service,
        rrule="FREQ=DAILY",
        starts_on=organization.today(),
    )
    materialize_plan(plan)
    return (
        Job.objects.filter(plan=plan, scheduled_start__gt=timezone.now())
        .order_by("scheduled_start")
        .first()
    )


@pytest.mark.django_db
class TestAFinishedVisitIsNotEditedInPlace:
    @pytest.mark.parametrize("status", TERMINAL_STATUSES)
    def test_rescheduling_one_is_a_409(self, dispatcher_client, job, status):
        in_status(job, status)
        before = job.scheduled_start

        response = dispatcher_client.patch(detail(job), move_payload(job), format="json")

        job.refresh_from_db()
        assert response.status_code == 409
        assert "reopen" in response.json()["detail"].lower()
        assert response.json()["status"] == status
        assert job.scheduled_start == before

    @pytest.mark.parametrize("status", TERMINAL_STATUSES)
    def test_it_cannot_be_handed_to_another_customer_or_repriced(
        self, dispatcher_client, organization, job, customer, status
    ):
        """
        The case that made this a rule: an invoiced visit is always a finished
        one, and re-pointing it leaves an invoice naming somebody else's visit.
        """
        in_status(job, status)
        stranger = CustomerFactory(organization=organization)
        elsewhere = ServiceLocationFactory(organization=organization, customer=stranger)

        response = dispatcher_client.patch(
            detail(job),
            {
                "customer": str(stranger.id),
                "location": str(elsewhere.id),
                "price_cents": 1,
                "notes": "rewritten",
            },
            format="json",
        )

        job.refresh_from_db()
        assert response.status_code == 409
        assert job.customer_id == customer.id
        assert job.price_cents == 15000
        assert job.notes == ""

    def test_a_full_update_is_refused_the_same_way(
        self, dispatcher_client, job, customer, location, service
    ):
        in_status(job, JobStatus.COMPLETE)
        payload = {
            "customer": str(customer.id),
            "location": str(location.id),
            "service": str(service.id),
            **move_payload(job),
        }

        assert dispatcher_client.put(detail(job), payload, format="json").status_code == 409

    def test_reopening_it_is_the_way_to_change_it(self, dispatcher_client, job):
        in_status(job, JobStatus.CANCELLED)

        dispatcher_client.post(status_url(job), {"status": "scheduled"}, format="json")
        response = dispatcher_client.patch(detail(job), move_payload(job), format="json")

        assert response.status_code == 200

    @pytest.mark.parametrize("status", UNFINISHED)
    def test_a_visit_that_is_not_finished_can_be_edited(self, dispatcher_client, job, status):
        in_status(job, status)

        response = dispatcher_client.patch(detail(job), {"notes": "Bring a ladder."}, format="json")

        assert response.status_code == 200
        assert response.json()["notes"] == "Bring a ladder."

    def test_recorded_time_does_not_stop_an_edit(
        self, dispatcher_client, organization, job, cleaner
    ):
        """
        Running long is the ordinary reason to move an end time, and by then
        somebody has clocked in. Only finishing the visit closes it to edits.
        """
        TimeEntryFactory(organization=organization, job=job, user=cleaner)
        in_status(job, JobStatus.IN_PROGRESS)
        later = job.scheduled_end + dt.timedelta(hours=1)

        response = dispatcher_client.patch(
            detail(job), {"scheduled_end": later.isoformat()}, format="json"
        )

        job.refresh_from_db()
        assert response.status_code == 200
        assert job.scheduled_end == later


@pytest.mark.django_db
class TestDeletingIsForAVisitBookedByMistake:
    def test_a_scheduled_one_off_can_be_deleted(self, dispatcher_client, job):
        assert dispatcher_client.delete(detail(job)).status_code == 204
        assert not Job.objects.filter(pk=job.pk).exists()

    @pytest.mark.parametrize(
        "status",
        [status for status in JobStatus.values if status != JobStatus.SCHEDULED],
    )
    def test_one_that_has_moved_on_cannot(self, dispatcher_client, job, status):
        in_status(job, status)

        response = dispatcher_client.delete(detail(job))

        assert response.status_code == 409
        assert "still scheduled" in response.json()["detail"]
        assert response.json()["status"] == status
        assert Job.objects.filter(pk=job.pk).exists()

    def test_a_plan_visit_is_cancelled_not_deleted(self, dispatcher_client, plan_job):
        """It would be back on the board after tonight's materialization."""
        response = dispatcher_client.delete(detail(plan_job))

        assert response.status_code == 409
        assert "recurring plan" in response.json()["detail"]
        assert "cancel" in response.json()["detail"].lower()
        assert Job.objects.filter(pk=plan_job.pk).exists()

    def test_a_cancelled_plan_visit_cannot_be_deleted_back_onto_the_board(
        self, dispatcher_client, plan_job
    ):
        """
        The reproduction behind the rule. Before it, this DELETE answered 204
        and the next materialization recreated the visit as `scheduled`: the
        customer called it off and the crew turned up anyway (ADR-021).
        """
        dispatcher_client.post(
            status_url(plan_job),
            {"status": "cancelled", "reason": "Customer is away."},
            format="json",
        )

        response = dispatcher_client.delete(detail(plan_job))
        created = materialize_plan(plan_job.plan)

        occurrence = Job.objects.filter(
            plan=plan_job.plan, plan_occurrence=plan_job.plan_occurrence
        )
        assert response.status_code == 409
        assert created == 0
        assert [row.status for row in occurrence] == [JobStatus.CANCELLED]

    def test_a_reopened_visit_with_recorded_time_still_cannot(
        self, dispatcher_client, organization, job, cleaner
    ):
        """Scheduled again, but the hours are still on it."""
        TimeEntryFactory(
            organization=organization,
            job=job,
            user=cleaner,
            clock_out=timezone.now() + dt.timedelta(hours=1),
        )

        response = dispatcher_client.delete(detail(job))

        assert response.status_code == 409
        assert "recorded time" in response.json()["detail"]


@pytest.mark.django_db
class TestPublishedActions:
    """
    `available_actions` is what the page draws Edit and Delete from. It is
    never worked out in the client (ADR-023).
    """

    def test_a_scheduled_one_off_offers_both(self, dispatcher_client, job):
        row = dispatcher_client.get(detail(job)).json()

        assert row["available_actions"] == ["edit", "delete"]

    def test_a_plan_visit_offers_edit_only(self, dispatcher_client, plan_job):
        row = dispatcher_client.get(detail(plan_job)).json()

        assert row["available_actions"] == ["edit"]

    @pytest.mark.parametrize("status", TERMINAL_STATUSES)
    def test_a_finished_visit_offers_nothing(self, dispatcher_client, job, status):
        in_status(job, status)

        assert dispatcher_client.get(detail(job)).json()["available_actions"] == []

    def test_recorded_time_withdraws_delete(self, dispatcher_client, organization, job, cleaner):
        TimeEntryFactory(organization=organization, job=job, user=cleaner)

        assert dispatcher_client.get(detail(job)).json()["available_actions"] == ["edit"]

    def test_a_cleaner_is_offered_nothing(self, cleaner_client, assigned_job):
        assert cleaner_client.get(detail(assigned_job)).json()["available_actions"] == []

    def test_a_customer_is_offered_nothing(self, customer_client, job):
        assert customer_client.get(detail(job)).json()["available_actions"] == []

    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.DISPATCHER])
    def test_every_dispatcher_tier_is_offered_them(
        self, organization, make_member, make_client, job, role
    ):
        client = make_client(make_member(organization, role=role, email=f"{role}@example.com"))

        assert client.get(detail(job)).json()["available_actions"] == ["edit", "delete"]

    def test_the_response_to_a_write_carries_the_new_answer(self, dispatcher_client, job):
        """So a page that just cancelled a visit stops offering Edit without a reload."""
        response = dispatcher_client.post(
            status_url(job), {"status": "cancelled", "reason": "Booked twice."}, format="json"
        )

        assert response.json()["available_actions"] == []

    def test_the_list_costs_no_query_per_row(
        self, dispatcher_client, organization, customer, location, service
    ):
        def queries_for_list():
            with CaptureQueriesContext(connection) as captured:
                assert dispatcher_client.get(LIST).status_code == 200
            return len(captured)

        JobFactory(organization=organization, customer=customer, location=location, service=service)
        one = queries_for_list()

        for _ in range(4):
            JobFactory(
                organization=organization, customer=customer, location=location, service=service
            )

        assert queries_for_list() == one

    @pytest.mark.parametrize("status", JobStatus.values)
    @pytest.mark.parametrize("source", ["one_off", "plan"])
    @pytest.mark.parametrize("worked", [False, True])
    def test_what_is_published_is_exactly_what_is_accepted(
        self, dispatcher_client, organization, cleaner, job, plan_job, status, source, worked
    ):
        """
        Every state a visit can be in. If the list and the server ever
        disagree, a page offers a button that 409s or hides one that works.
        """
        target = in_status(job if source == "one_off" else plan_job, status)
        if worked:
            TimeEntryFactory(
                organization=organization,
                job=target,
                user=cleaner,
                clock_out=timezone.now() + dt.timedelta(hours=1),
            )

        offered = dispatcher_client.get(detail(target)).json()["available_actions"]

        edit = dispatcher_client.patch(detail(target), {"notes": "Checked."}, format="json")
        assert (edit.status_code == 200) == ("edit" in offered)
        assert edit.status_code in (200, 409)

        delete = dispatcher_client.delete(detail(target))
        assert (delete.status_code == 204) == ("delete" in offered)
        assert delete.status_code in (204, 409)
