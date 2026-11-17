"""
Visit booking in an organization that has been deactivated (ADR-028).

A lapsed or deactivated organization is read-only: `IsOrgMember` refuses every
write, and the nightly materializer skips it. Two things follow for the work
on this branch, and both are proved here rather than assumed.

* Booking, editing, deleting and reopening a visit are writes. They are
  refused the way every other write is, and the job stops *offering* them,
  because a button that can only answer 403 is a lie the page would tell.
* While the organization is away its plans fall behind the horizon. When it
  comes back the materializer catches up, and the visits a dispatcher had
  touched before the lapse (kept by their stamp, or cancelled) must still
  account for their days: nothing is duplicated and nothing comes back.
"""

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from base.permissions import ORGANIZATION_INACTIVE
from scheduling.enums import JobStatus
from scheduling.models import Job
from scheduling.services import materialize_plan
from scheduling.tasks import materialize_all_organizations, materialize_organization
from scheduling.tests.factories import RecurringPlanFactory

LIST = reverse("scheduling:job-list")


def detail(job):
    return reverse("scheduling:job-detail", args=[job.id])


def status_url(job):
    return reverse("scheduling:job-status", args=[job.id])


def plan_url(plan):
    return reverse("scheduling:plan-detail", args=[plan.id])


def planned_day(job):
    return job.plan_occurrence.astimezone(job.organization.tz).date()


def deactivate(organization):
    organization.is_active = False
    organization.save()


def reactivate(organization):
    organization.is_active = True
    organization.save()


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
class TestADeactivatedOrganizationIsReadOnly:
    def test_a_visit_cannot_be_booked(self, dispatcher_client, organization, payload):
        deactivate(organization)

        response = dispatcher_client.post(LIST, payload, format="json")

        assert response.status_code == 403
        assert response.json()["detail"] == ORGANIZATION_INACTIVE
        assert not Job.objects.exists()

    def test_a_visit_cannot_be_moved_or_edited(self, dispatcher_client, organization, job):
        before = job.scheduled_start
        deactivate(organization)
        later = before + dt.timedelta(days=1)

        patched = dispatcher_client.patch(
            detail(job),
            {
                "scheduled_start": later.isoformat(),
                "scheduled_end": (later + dt.timedelta(hours=2)).isoformat(),
                "notes": "Changed.",
            },
            format="json",
        )

        job.refresh_from_db()
        assert patched.status_code == 403
        assert patched.json()["detail"] == ORGANIZATION_INACTIVE
        assert job.scheduled_start == before
        assert job.notes == ""

    def test_a_visit_cannot_be_replaced(self, dispatcher_client, organization, job, payload):
        deactivate(organization)

        assert dispatcher_client.put(detail(job), payload, format="json").status_code == 403

    def test_a_visit_cannot_be_deleted(self, dispatcher_client, organization, job):
        deactivate(organization)

        response = dispatcher_client.delete(detail(job))

        assert response.status_code == 403
        assert response.json()["detail"] == ORGANIZATION_INACTIVE
        assert Job.objects.filter(pk=job.pk).exists()

    def test_a_finished_visit_cannot_be_reopened(self, dispatcher_client, organization, job):
        job.status = JobStatus.COMPLETE
        job.save()
        deactivate(organization)

        response = dispatcher_client.post(status_url(job), {"status": "scheduled"}, format="json")

        job.refresh_from_db()
        assert response.status_code == 403
        assert response.json()["detail"] == ORGANIZATION_INACTIVE
        assert job.status == JobStatus.COMPLETE

    def test_a_plan_cannot_be_edited_or_regenerated(self, dispatcher_client, organization):
        plan = RecurringPlanFactory(organization=organization)
        deactivate(organization)

        edit = dispatcher_client.patch(
            plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json"
        )
        regenerate = dispatcher_client.post(reverse("scheduling:plan-regenerate", args=[plan.id]))

        assert edit.status_code == regenerate.status_code == 403

    def test_the_visit_can_still_be_read(self, dispatcher_client, organization, job):
        deactivate(organization)

        assert dispatcher_client.get(detail(job)).status_code == 200
        assert dispatcher_client.get(LIST).json()["count"] == 1

    def test_it_is_offered_nothing_it_would_be_refused(
        self, dispatcher_client, cleaner_client, organization, assigned_job
    ):
        """Published is what is accepted, here as everywhere."""
        before = dispatcher_client.get(detail(assigned_job)).json()
        assert before["available_actions"] == ["edit", "delete"]
        assert before["next_statuses"] != []

        deactivate(organization)

        for client in (dispatcher_client, cleaner_client):
            row = client.get(detail(assigned_job)).json()
            assert row["available_actions"] == []
            assert row["next_statuses"] == []

    def test_a_superuser_putting_things_right_is_still_offered_them(
        self, superuser, make_client, organization, job
    ):
        """`IsOrgMember` exempts a superuser, so the offer has to as well."""
        deactivate(organization)
        client = make_client(superuser)
        client.credentials(HTTP_X_ORGANIZATION=str(organization.id))

        row = client.get(detail(job)).json()
        response = client.patch(detail(job), {"notes": "Fixed by support."}, format="json")

        assert row["available_actions"] == ["edit", "delete"]
        assert response.status_code == 200

    def test_coming_back_restores_all_of_it(self, dispatcher_client, organization, job, payload):
        deactivate(organization)
        reactivate(organization)

        assert dispatcher_client.get(detail(job)).json()["available_actions"] == ["edit", "delete"]
        assert dispatcher_client.post(LIST, payload, format="json").status_code == 201
        assert dispatcher_client.delete(detail(job)).status_code == 204


@pytest.mark.django_db
class TestComingBackAfterALapse:
    """
    The materializer skipped the organization for a while. Catching up must
    not disturb what a dispatcher did before it lapsed.
    """

    @pytest.fixture
    def plan(self, organization, customer, location, service):
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
    def touched(self, dispatcher_client, plan):
        """A visit kept for its price and one that was cancelled, both well ahead."""
        visits = list(
            Job.objects.filter(
                plan=plan, scheduled_start__gt=timezone.now() + dt.timedelta(days=40)
            ).order_by("scheduled_start")
        )
        repriced, cancelled = visits[0], visits[1]
        dispatcher_client.patch(detail(repriced), {"price_cents": 99900}, format="json")
        dispatcher_client.post(
            status_url(cancelled),
            {"status": "cancelled", "reason": "Customer is away."},
            format="json",
        )
        return repriced, cancelled

    def test_nothing_is_materialized_while_it_is_away(self, organization, plan):
        before = Job.objects.filter(plan=plan).count()
        deactivate(organization)

        assert materialize_all_organizations() == 0
        assert materialize_organization(str(organization.id)) == 0
        assert Job.objects.filter(plan=plan).count() == before

    def test_the_horizon_catches_up_without_a_second_visit_on_any_day(
        self, organization, plan, touched, monkeypatch
    ):
        repriced, cancelled = touched
        deactivate(organization)
        reactivate(organization)

        # Thirty days on: the nightly run has a month of horizon to make up.
        later = organization.today() + dt.timedelta(days=30)
        monkeypatch.setattr(type(organization), "today", lambda self: later)
        created = materialize_organization(str(organization.id))

        days = [planned_day(job) for job in Job.objects.filter(plan=plan)]
        assert created == 30
        assert len(days) == len(set(days))
        # Every visit the catch-up made carries its stamp.
        assert not Job.objects.filter(plan=plan, planned_end__isnull=True).exists()

        repriced.refresh_from_db()
        cancelled.refresh_from_db()
        assert repriced.price_cents == 99900
        assert cancelled.status == JobStatus.CANCELLED

        # And a second night adds nothing.
        assert materialize_organization(str(organization.id)) == 0

    def test_a_plan_edit_after_coming_back_still_keeps_what_was_touched(
        self, dispatcher_client, organization, plan, touched
    ):
        repriced, cancelled = touched
        deactivate(organization)
        reactivate(organization)
        materialize_organization(str(organization.id))

        body = dispatcher_client.patch(
            plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json"
        ).json()

        repriced.refresh_from_db()
        cancelled.refresh_from_db()
        days = [planned_day(job) for job in Job.objects.filter(plan=plan)]
        assert body["kept"] == 2
        assert body["regenerated"] > 0
        assert len(days) == len(set(days))
        assert repriced.deleted_at is None and repriced.price_cents == 99900
        assert cancelled.deleted_at is None and cancelled.status == JobStatus.CANCELLED
