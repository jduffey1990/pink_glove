"""
Clocking in and out, and the correction path for dispatchers.

Clock-in drags a SCHEDULED or EN_ROUTE job into IN_PROGRESS on purpose: someone
is on site with the clock running. Making the cleaner press a second button to
say so is how statuses end up wrong.
"""

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from scheduling.enums import JobStatus
from scheduling.models import TimeEntry
from scheduling.tests.factories import JobFactory

LIST = reverse("scheduling:timeentry-list")


def clock_in_url(job):
    return reverse("scheduling:job-clock-in", args=[job.id])


def clock_out_url(job):
    return reverse("scheduling:job-clock-out", args=[job.id])


def entry_detail(entry):
    return reverse("scheduling:timeentry-detail", args=[entry.id])


@pytest.mark.django_db
class TestClockIn:
    def test_an_assigned_cleaner_can_clock_in(self, cleaner_client, assigned_job, cleaner):
        response = cleaner_client.post(clock_in_url(assigned_job))

        assert response.status_code == 200
        assert TimeEntry.objects.filter(job=assigned_job, user=cleaner).exists()

    def test_it_moves_a_scheduled_job_to_in_progress(self, cleaner_client, assigned_job):
        cleaner_client.post(clock_in_url(assigned_job))

        assigned_job.refresh_from_db()
        assert assigned_job.status == JobStatus.IN_PROGRESS

    def test_it_moves_an_en_route_job_to_in_progress(self, cleaner_client, assigned_job):
        assigned_job.status = JobStatus.EN_ROUTE
        assigned_job.save()

        cleaner_client.post(clock_in_url(assigned_job))

        assigned_job.refresh_from_db()
        assert assigned_job.status == JobStatus.IN_PROGRESS

    def test_clocking_in_twice_is_a_409(self, cleaner_client, assigned_job):
        cleaner_client.post(clock_in_url(assigned_job))

        response = cleaner_client.post(clock_in_url(assigned_job))

        assert response.status_code == 409
        assert TimeEntry.objects.filter(job=assigned_job).count() == 1

    def test_a_cleaner_cannot_clock_into_a_job_they_are_not_on(self, cleaner_client, job):
        assert cleaner_client.post(clock_in_url(job)).status_code == 404

    def test_clocking_into_a_finished_job_is_a_409(self, cleaner_client, assigned_job):
        assigned_job.status = JobStatus.COMPLETE
        assigned_job.save()

        response = cleaner_client.post(clock_in_url(assigned_job))

        assert response.status_code == 409

    def test_two_cleaners_can_be_clocked_into_one_job(
        self, cleaner_client, make_client, assigned_job, other_cleaner, organization
    ):
        from scheduling.models import JobAssignment

        JobAssignment.objects.create(
            organization=organization, job=assigned_job, user=other_cleaner
        )
        cleaner_client.post(clock_in_url(assigned_job))

        response = make_client(other_cleaner).post(clock_in_url(assigned_job))

        assert response.status_code == 200
        assert TimeEntry.objects.filter(job=assigned_job, clock_out__isnull=True).count() == 2

    def test_a_dispatcher_can_clock_in_without_being_assigned(
        self, dispatcher_client, job, dispatcher
    ):
        """A dispatcher covering a visit themselves is ordinary."""
        response = dispatcher_client.post(clock_in_url(job))

        assert response.status_code == 200
        assert TimeEntry.objects.filter(job=job, user=dispatcher).exists()

    def test_a_customer_cannot_clock_in(self, customer_client, job):
        assert customer_client.post(clock_in_url(job)).status_code == 403


@pytest.mark.django_db
class TestClockOut:
    def test_it_closes_the_open_entry(self, cleaner_client, assigned_job, cleaner):
        cleaner_client.post(clock_in_url(assigned_job))

        response = cleaner_client.post(clock_out_url(assigned_job))

        entry = TimeEntry.objects.get(job=assigned_job, user=cleaner)
        assert response.status_code == 200
        assert entry.clock_out is not None

    def test_clocking_out_without_clocking_in_is_a_409(self, cleaner_client, assigned_job):
        assert cleaner_client.post(clock_out_url(assigned_job)).status_code == 409

    def test_clocking_out_twice_is_a_409(self, cleaner_client, assigned_job):
        cleaner_client.post(clock_in_url(assigned_job))
        cleaner_client.post(clock_out_url(assigned_job))

        assert cleaner_client.post(clock_out_url(assigned_job)).status_code == 409

    def test_the_status_is_left_alone(self, cleaner_client, assigned_job):
        """
        Whether the visit is finished is a separate judgement. A cleaner who
        stops for lunch has not completed the job.
        """
        cleaner_client.post(clock_in_url(assigned_job))
        cleaner_client.post(clock_out_url(assigned_job))

        assigned_job.refresh_from_db()
        assert assigned_job.status == JobStatus.IN_PROGRESS

    def test_a_cleaner_can_clock_back_in_after_a_break(self, cleaner_client, assigned_job):
        cleaner_client.post(clock_in_url(assigned_job))
        cleaner_client.post(clock_out_url(assigned_job))

        response = cleaner_client.post(clock_in_url(assigned_job))

        assert response.status_code == 200
        assert TimeEntry.objects.filter(job=assigned_job).count() == 2


@pytest.mark.django_db
class TestReadingTimeEntries:
    def test_a_cleaner_sees_only_their_own(
        self, cleaner_client, assigned_job, organization, other_cleaner
    ):
        TimeEntry.objects.create(
            organization=organization,
            job=assigned_job,
            user=other_cleaner,
            clock_in=timezone.now(),
        )
        cleaner_client.post(clock_in_url(assigned_job))

        response = cleaner_client.get(LIST)

        assert response.json()["count"] == 1

    def test_a_dispatcher_sees_everyones(
        self, dispatcher_client, cleaner_client, assigned_job, organization, other_cleaner
    ):
        TimeEntry.objects.create(
            organization=organization,
            job=assigned_job,
            user=other_cleaner,
            clock_in=timezone.now(),
        )
        cleaner_client.post(clock_in_url(assigned_job))

        assert dispatcher_client.get(LIST).json()["count"] == 2

    def test_another_organizations_entries_are_invisible(
        self, dispatcher_client, other_organization, rival_cleaner
    ):
        rival_job = JobFactory(organization=other_organization)
        TimeEntry.objects.create(
            organization=other_organization,
            job=rival_job,
            user=rival_cleaner,
            clock_in=timezone.now(),
        )

        assert dispatcher_client.get(LIST).json()["count"] == 0

    def test_a_customer_sees_nothing(self, customer_client, assigned_job, organization, cleaner):
        TimeEntry.objects.create(
            organization=organization, job=assigned_job, user=cleaner, clock_in=timezone.now()
        )

        assert customer_client.get(LIST).status_code == 403

    def test_filter_by_job(
        self, dispatcher_client, cleaner_client, assigned_job, organization, other_cleaner
    ):
        cleaner_client.post(clock_in_url(assigned_job))
        TimeEntry.objects.create(
            organization=organization,
            job=JobFactory(organization=organization),
            user=other_cleaner,
            clock_in=timezone.now(),
        )

        response = dispatcher_client.get(LIST, {"job": str(assigned_job.id)})

        assert response.json()["count"] == 1


@pytest.mark.django_db
class TestCorrectingTimeEntries:
    def test_a_dispatcher_can_correct_the_times(
        self, dispatcher_client, cleaner_client, assigned_job, cleaner
    ):
        """The commonest real correction: somebody forgot to clock out."""
        cleaner_client.post(clock_in_url(assigned_job))
        entry = TimeEntry.objects.get(job=assigned_job, user=cleaner)
        corrected = entry.clock_in + dt.timedelta(hours=3)

        response = dispatcher_client.patch(
            entry_detail(entry), {"clock_out": corrected.isoformat()}, format="json"
        )

        entry.refresh_from_db()
        assert response.status_code == 200
        assert entry.clock_out == corrected

    def test_a_cleaner_cannot_correct_their_own_hours(self, cleaner_client, assigned_job, cleaner):
        cleaner_client.post(clock_in_url(assigned_job))
        entry = TimeEntry.objects.get(job=assigned_job, user=cleaner)

        response = cleaner_client.patch(
            entry_detail(entry),
            {"clock_out": (entry.clock_in + dt.timedelta(hours=9)).isoformat()},
            format="json",
        )

        assert response.status_code == 403

    def test_a_clock_out_before_the_clock_in_is_refused(
        self, dispatcher_client, cleaner_client, assigned_job, cleaner
    ):
        cleaner_client.post(clock_in_url(assigned_job))
        entry = TimeEntry.objects.get(job=assigned_job, user=cleaner)

        response = dispatcher_client.patch(
            entry_detail(entry),
            {"clock_out": (entry.clock_in - dt.timedelta(hours=1)).isoformat()},
            format="json",
        )

        assert response.status_code == 400

    def test_entries_cannot_be_created_directly(self, dispatcher_client, job):
        """Creation goes through clock-in, where the rules live."""
        response = dispatcher_client.post(LIST, {"job": str(job.id)}, format="json")

        assert response.status_code == 405

    def test_the_user_cannot_be_reassigned(
        self, dispatcher_client, cleaner_client, assigned_job, cleaner, other_cleaner
    ):
        cleaner_client.post(clock_in_url(assigned_job))
        entry = TimeEntry.objects.get(job=assigned_job, user=cleaner)

        dispatcher_client.patch(entry_detail(entry), {"user": str(other_cleaner.id)}, format="json")

        entry.refresh_from_db()
        assert entry.user_id == cleaner.id
