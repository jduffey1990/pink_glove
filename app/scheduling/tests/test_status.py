"""
The job state machine and assignment.

Transitions are refused with 409 and the allowed next states in the body, so a
client that guessed wrong can render the right buttons without a second
request. The UI is specified to read its buttons from that body, which makes
the shape of the 409 part of the contract, not an error detail.
"""

import pytest
from django.urls import reverse

from scheduling.enums import JobStatus
from scheduling.models import JobAssignment
from scheduling.tests.factories import JobFactory


def status_url(job):
    return reverse("scheduling:job-status", args=[job.id])


def assign_url(job):
    return reverse("scheduling:job-assign", args=[job.id])


def unassign_url(job):
    return reverse("scheduling:job-unassign", args=[job.id])


def _move(client, job, to_status, reason=""):
    return client.post(status_url(job), {"status": to_status, "reason": reason}, format="json")


@pytest.mark.django_db
class TestAllowedTransitions:
    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (JobStatus.SCHEDULED, JobStatus.EN_ROUTE),
            (JobStatus.SCHEDULED, JobStatus.IN_PROGRESS),
            (JobStatus.EN_ROUTE, JobStatus.IN_PROGRESS),
            (JobStatus.EN_ROUTE, JobStatus.SCHEDULED),
            (JobStatus.IN_PROGRESS, JobStatus.COMPLETE),
        ],
    )
    def test_each_forward_move_is_accepted(self, dispatcher_client, job, start, target):
        job.status = start
        job.save()

        response = _move(dispatcher_client, job, target)

        job.refresh_from_db()
        assert response.status_code == 200
        assert job.status == target

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (JobStatus.SCHEDULED, JobStatus.COMPLETE),
            (JobStatus.COMPLETE, JobStatus.IN_PROGRESS),
            (JobStatus.CANCELLED, JobStatus.COMPLETE),
            (JobStatus.IN_PROGRESS, JobStatus.EN_ROUTE),
            (JobStatus.IN_PROGRESS, JobStatus.CANCELLED),
        ],
    )
    def test_each_illegal_move_is_a_409(self, dispatcher_client, job, start, target):
        job.status = start
        job.save()

        response = _move(dispatcher_client, job, target, reason="because")

        job.refresh_from_db()
        assert response.status_code == 409
        assert job.status == start

    def test_the_409_names_the_allowed_next_states(self, dispatcher_client, job):
        """The UI renders its buttons from this list."""
        job.status = JobStatus.IN_PROGRESS
        job.save()

        body = _move(dispatcher_client, job, JobStatus.EN_ROUTE).json()

        assert body["status"] == JobStatus.IN_PROGRESS
        assert set(body["allowed"]) == {JobStatus.COMPLETE, JobStatus.NO_ACCESS}

    def test_an_unknown_status_is_a_400(self, dispatcher_client, job):
        response = _move(dispatcher_client, job, "teleported")

        assert response.status_code == 400


@pytest.mark.django_db
class TestReasons:
    def test_cancelling_requires_a_reason(self, dispatcher_client, job):
        response = _move(dispatcher_client, job, JobStatus.CANCELLED)

        job.refresh_from_db()
        assert response.status_code == 400
        assert "reason" in response.json()
        assert job.status == JobStatus.SCHEDULED

    def test_a_cancellation_reason_is_stored(self, dispatcher_client, job):
        _move(dispatcher_client, job, JobStatus.CANCELLED, reason="Customer rebooked")

        job.refresh_from_db()
        assert job.status == JobStatus.CANCELLED
        assert job.cancellation_reason == "Customer rebooked"

    def test_no_access_requires_a_reason_too(self, dispatcher_client, job):
        """ "We could not get in" is a distinct outcome and needs the detail."""
        assert _move(dispatcher_client, job, JobStatus.NO_ACCESS).status_code == 400

    def test_no_access_records_what_happened(self, cleaner_client, assigned_job):
        _move(cleaner_client, assigned_job, JobStatus.NO_ACCESS, reason="Gate code refused")

        assigned_job.refresh_from_db()
        assert assigned_job.status == JobStatus.NO_ACCESS
        assert assigned_job.cancellation_reason == "Gate code refused"

    def test_a_blank_reason_does_not_count(self, dispatcher_client, job):
        assert _move(dispatcher_client, job, JobStatus.CANCELLED, reason="   ").status_code == 400


@pytest.mark.django_db
class TestWhoMayTransition:
    def test_an_assigned_cleaner_may_move_their_own_job(self, cleaner_client, assigned_job):
        response = _move(cleaner_client, assigned_job, JobStatus.EN_ROUTE)

        assert response.status_code == 200

    def test_a_cleaner_may_not_cancel(self, cleaner_client, assigned_job):
        """Cancelling is a commercial decision, not a field one."""
        response = _move(cleaner_client, assigned_job, JobStatus.CANCELLED, reason="cba")

        assigned_job.refresh_from_db()
        assert response.status_code == 409
        assert assigned_job.status == JobStatus.SCHEDULED

    def test_a_cleaner_cannot_touch_a_job_they_are_not_on(self, cleaner_client, job):
        assert _move(cleaner_client, job, JobStatus.EN_ROUTE).status_code == 404

    def test_a_customer_cannot_move_a_job(self, customer_client, job):
        assert _move(customer_client, job, JobStatus.EN_ROUTE).status_code == 403

    def test_a_dispatcher_can_reopen_a_finished_job(self, dispatcher_client, job):
        job.status = JobStatus.COMPLETE
        job.save()

        response = _move(dispatcher_client, job, JobStatus.SCHEDULED)

        job.refresh_from_db()
        assert response.status_code == 200
        assert job.status == JobStatus.SCHEDULED

    def test_a_cleaner_cannot_reopen_a_finished_job(self, cleaner_client, assigned_job):
        assigned_job.status = JobStatus.COMPLETE
        assigned_job.save()

        response = _move(cleaner_client, assigned_job, JobStatus.SCHEDULED)

        assigned_job.refresh_from_db()
        assert response.status_code == 409
        assert assigned_job.status == JobStatus.COMPLETE

    def test_the_timestamp_is_recorded(self, dispatcher_client, job):
        assert job.status_changed_at is None

        _move(dispatcher_client, job, JobStatus.EN_ROUTE)

        job.refresh_from_db()
        assert job.status_changed_at is not None


@pytest.mark.django_db
class TestPublishedNextStatuses:
    """
    A job says which moves its reader may make. The pages draw their buttons
    from this, so what matters is that it is the same answer `transition_job`
    gives -- the last test tries every published move, and every unpublished
    one, against the real endpoint.
    """

    def _detail(self, client, job):
        return client.get(reverse("scheduling:job-detail", args=[job.id])).json()

    def _offered(self, client, job):
        return [row["status"] for row in self._detail(client, job)["next_statuses"]]

    def test_a_dispatcher_is_offered_the_whole_machine(self, dispatcher_client, job):
        assert set(self._offered(dispatcher_client, job)) == {
            "en_route",
            "in_progress",
            "cancelled",
            "no_access",
        }

    def test_a_cleaner_is_not_offered_cancel(self, cleaner_client, assigned_job):
        assert "cancelled" not in self._offered(cleaner_client, assigned_job)

    @pytest.mark.parametrize(
        "finished", [JobStatus.COMPLETE, JobStatus.CANCELLED, JobStatus.NO_ACCESS]
    )
    def test_a_finished_job_offers_a_cleaner_nothing(
        self, cleaner_client, dispatcher_client, assigned_job, finished
    ):
        assigned_job.status = finished
        assigned_job.save()

        body = self._detail(cleaner_client, assigned_job)

        assert body["is_terminal"] is True
        assert body["next_statuses"] == []
        assert self._offered(dispatcher_client, assigned_job) == ["scheduled"]

    def test_the_moves_that_need_a_reason_say_so(self, dispatcher_client, job):
        needs_reason = {
            row["status"]
            for row in self._detail(dispatcher_client, job)["next_statuses"]
            if row["reason_required"]
        }

        assert needs_reason == {"cancelled", "no_access"}

    @pytest.mark.parametrize("start", JobStatus.values)
    @pytest.mark.parametrize("who", ["dispatcher", "cleaner"])
    def test_what_is_published_is_exactly_what_is_accepted(
        self, dispatcher_client, cleaner_client, assigned_job, start, who
    ):
        client = dispatcher_client if who == "dispatcher" else cleaner_client
        assigned_job.status = start
        assigned_job.save()
        offered = self._offered(client, assigned_job)

        for target in JobStatus.values:
            assigned_job.status = start
            assigned_job.save()

            accepted = _move(client, assigned_job, target, reason="Because").status_code == 200

            assert accepted == (target in offered), f"{who}: {start} -> {target}"


@pytest.mark.django_db
class TestAssignment:
    def test_a_dispatcher_can_assign_a_cleaner(self, dispatcher_client, job, cleaner):
        response = dispatcher_client.post(assign_url(job), {"user": str(cleaner.id)}, format="json")

        assert response.status_code == 200
        assert JobAssignment.objects.filter(job=job, user=cleaner).exists()

    def test_assigning_twice_is_harmless(self, dispatcher_client, job, cleaner):
        payload = {"user": str(cleaner.id)}
        dispatcher_client.post(assign_url(job), payload, format="json")
        response = dispatcher_client.post(assign_url(job), payload, format="json")

        assert response.status_code == 200
        assert JobAssignment.objects.filter(job=job, user=cleaner).count() == 1

    def test_a_rival_organizations_cleaner_is_refused(self, dispatcher_client, job, rival_cleaner):
        """
        JobAssignment.user is a CustomUser, not a TenantModel, so the model's
        cross-organization check does not cover this. The service does.
        """
        response = dispatcher_client.post(
            assign_url(job), {"user": str(rival_cleaner.id)}, format="json"
        )

        assert response.status_code == 400
        assert "user" in response.json()
        assert not JobAssignment.objects.filter(job=job, user=rival_cleaner).exists()

    def test_a_user_with_no_membership_anywhere_is_refused(self, dispatcher_client, job, user):
        response = dispatcher_client.post(assign_url(job), {"user": str(user.id)}, format="json")

        assert response.status_code == 400

    def test_a_customer_cannot_be_assigned_to_a_job(self, dispatcher_client, job, customer_user):
        response = dispatcher_client.post(
            assign_url(job), {"user": str(customer_user.id)}, format="json"
        )

        assert response.status_code == 400

    def test_assigning_to_a_finished_job_is_a_409(self, dispatcher_client, job, cleaner):
        job.status = JobStatus.COMPLETE
        job.save()

        response = dispatcher_client.post(assign_url(job), {"user": str(cleaner.id)}, format="json")

        assert response.status_code == 409

    def test_an_unknown_user_id_is_a_400(self, dispatcher_client, job):
        import uuid

        response = dispatcher_client.post(
            assign_url(job), {"user": str(uuid.uuid4())}, format="json"
        )

        assert response.status_code == 400

    def test_a_cleaner_cannot_assign_anyone(self, cleaner_client, assigned_job, other_cleaner):
        response = cleaner_client.post(
            assign_url(assigned_job), {"user": str(other_cleaner.id)}, format="json"
        )

        assert response.status_code == 403

    def test_unassigning_removes_the_row(self, dispatcher_client, assigned_job, cleaner):
        response = dispatcher_client.post(
            unassign_url(assigned_job), {"user": str(cleaner.id)}, format="json"
        )

        assert response.status_code == 200
        assert not JobAssignment.objects.filter(job=assigned_job, user=cleaner).exists()

    def test_unassigning_somebody_who_was_never_on_it_is_harmless(
        self, dispatcher_client, job, cleaner
    ):
        response = dispatcher_client.post(
            unassign_url(job), {"user": str(cleaner.id)}, format="json"
        )

        assert response.status_code == 200

    def test_a_rival_organizations_job_is_a_404(
        self, dispatcher_client, other_organization, cleaner
    ):
        rival = JobFactory(organization=other_organization)

        response = dispatcher_client.post(
            assign_url(rival), {"user": str(cleaner.id)}, format="json"
        )

        assert response.status_code == 404
