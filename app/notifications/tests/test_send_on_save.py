"""
A visit booked or moved into its reminder window is reminded as soon as the
save commits (ADR-035), through a `post_save` receiver and the one-visit task.

The clock is pinned to NOW (18:00 Denver, past the reminder hour), so a visit
tomorrow is inside the window and one the day after is not. On-commit
callbacks only run when the test asks, which is how a rolled-back save is
shown to queue nothing.
"""

import datetime as dt

import pytest
from django.core import mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from notifications import signals, tasks
from notifications.models import VisitReminder
from notifications.tests.clock import NOW, TOMORROW_2PM
from scheduling.enums import JobStatus
from scheduling.models import Job
from scheduling.tests.factories import OrganizationFactory
from users.enums import Role

DAY_AFTER_TOMORROW = TOMORROW_2PM + dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: NOW)


@pytest.fixture
def queued(monkeypatch):
    """What the receiver hands the one-visit task, without running it."""
    calls = []
    monkeypatch.setattr(signals.send_visit_reminder, "delay", lambda *args: calls.append(args))
    return calls


@pytest.mark.django_db
class TestTheReceiver:
    def test_booking_a_visit_inside_the_window_reminds_it_at_once(
        self, org, make_visit, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            visit = make_visit(org, TOMORROW_2PM)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [visit.customer.email]
        assert VisitReminder.objects.get().job == visit

    def test_it_hands_the_task_the_organization_and_the_visit(
        self, org, make_visit, queued, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            visit = make_visit(org, TOMORROW_2PM)

        assert queued == [(str(org.id), str(visit.id))]

    def test_moving_a_visit_into_the_window_reminds_it(
        self, org, make_visit, django_capture_on_commit_callbacks
    ):
        visit = make_visit(org, DAY_AFTER_TOMORROW)

        with django_capture_on_commit_callbacks(execute=True):
            visit.scheduled_start = TOMORROW_2PM
            visit.scheduled_end = TOMORROW_2PM + dt.timedelta(hours=2)
            visit.save()

        assert len(mail.outbox) == 1

    def test_moving_a_reminded_visit_within_the_window_reminds_the_new_time(
        self, org, make_visit, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            visit = make_visit(org, TOMORROW_2PM)
        with django_capture_on_commit_callbacks(execute=True):
            visit.scheduled_start += dt.timedelta(hours=1)
            visit.scheduled_end += dt.timedelta(hours=1)
            visit.save()

        assert [m.subject.rsplit(" at ", 1)[1] for m in mail.outbox] == ["2:00 PM", "3:00 PM"]

    def test_saving_an_already_reminded_visit_sends_nothing_more(
        self, org, make_visit, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            visit = make_visit(org, TOMORROW_2PM)
        with django_capture_on_commit_callbacks(execute=True):
            visit.notes = "Bring the ladder."
            visit.save()

        assert len(mail.outbox) == 1

    def test_a_visit_far_off_or_already_started_queues_nothing(
        self, org, make_visit, queued, django_capture_on_commit_callbacks
    ):
        """The prefilter: no query for the organization, no task."""
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            make_visit(org, NOW + signals.LOOKAHEAD + dt.timedelta(minutes=1))
            make_visit(org, NOW - dt.timedelta(hours=1))

        assert callbacks == []
        assert queued == []

    def test_a_visit_near_but_outside_the_window_is_queued_and_not_sent(
        self, org, make_visit, django_capture_on_commit_callbacks
    ):
        """Inside the generous prefilter; the task asks `due_reminders` and says no."""
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            make_visit(org, DAY_AFTER_TOMORROW)

        assert len(callbacks) == 1
        assert mail.outbox == []
        assert not VisitReminder.objects.exists()

    def test_a_visit_that_is_not_scheduled_queues_nothing(
        self, org, make_visit, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks() as callbacks:
            make_visit(org, TOMORROW_2PM, status=JobStatus.CANCELLED)

        assert callbacks == []

    @pytest.mark.parametrize("change", [{"visit_reminders_enabled": False}, {"is_active": False}])
    def test_an_organization_that_does_not_send_queues_nothing(
        self, org, make_visit, django_capture_on_commit_callbacks, change
    ):
        for field, value in change.items():
            setattr(org, field, value)
        org.save()

        with django_capture_on_commit_callbacks() as callbacks:
            make_visit(org, TOMORROW_2PM)

        assert callbacks == []

    def test_a_rolled_back_booking_emails_nobody(
        self, org, make_visit, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with pytest.raises(RuntimeError), transaction.atomic():
                make_visit(org, TOMORROW_2PM)
                raise RuntimeError("the booking failed")

        assert callbacks == []
        assert mail.outbox == []

    def test_visits_a_plan_bulk_creates_are_left_to_the_hourly_task(
        self, org, make_visit, django_capture_on_commit_callbacks
    ):
        template = make_visit(org, DAY_AFTER_TOMORROW)

        with django_capture_on_commit_callbacks() as callbacks:
            Job.objects.bulk_create(
                [
                    Job(
                        organization=org,
                        customer=template.customer,
                        location=template.location,
                        service=template.service,
                        scheduled_start=TOMORROW_2PM,
                        scheduled_end=TOMORROW_2PM + dt.timedelta(hours=2),
                        price_cents=15000,
                    )
                ]
            )

        assert callbacks == []


@pytest.mark.django_db
class TestThroughTheApi:
    """The way a dispatcher actually does it."""

    def test_booking_tomorrow_through_the_api_emails_the_customer(
        self, org, make_member, make_visit, django_capture_on_commit_callbacks
    ):
        template = make_visit(org, DAY_AFTER_TOMORROW)
        dispatcher = make_member(org, Role.DISPATCHER)
        client = APIClient()
        client.force_login(dispatcher)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                reverse("scheduling:job-list"),
                {
                    "customer": str(template.customer.id),
                    "location": str(template.location.id),
                    "service": str(template.service.id),
                    "scheduled_start": TOMORROW_2PM.isoformat(),
                    "scheduled_end": (TOMORROW_2PM + dt.timedelta(hours=2)).isoformat(),
                },
                format="json",
            )

        assert response.status_code == 201
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [template.customer.email]


@pytest.mark.django_db
class TestTheOneVisitTask:
    """It re-checks everything, whatever the receiver thought."""

    def test_it_sends_a_due_visit_once(self, org, make_visit):
        visit = make_visit(org, TOMORROW_2PM)

        assert tasks.send_visit_reminder(str(org.id), str(visit.id)) is True
        assert tasks.send_visit_reminder(str(org.id), str(visit.id)) is False
        assert len(mail.outbox) == 1

    def test_a_visit_no_longer_due_is_not_sent(self, org, make_visit):
        visit = make_visit(org, DAY_AFTER_TOMORROW)

        assert tasks.send_visit_reminder(str(org.id), str(visit.id)) is False
        assert mail.outbox == []

    def test_a_visit_of_another_organization_is_not_found(self, org, make_visit):
        rival = OrganizationFactory(timezone="America/Denver", visit_reminders_enabled=True)
        theirs = make_visit(rival, TOMORROW_2PM)

        assert tasks.send_visit_reminder(str(org.id), str(theirs.id)) is False
        assert mail.outbox == []
        assert not VisitReminder.objects.exists()

    def test_an_organization_switched_off_since_the_save_sends_nothing(self, org, make_visit):
        visit = make_visit(org, TOMORROW_2PM)
        org.visit_reminders_enabled = False
        org.save()

        assert tasks.send_visit_reminder(str(org.id), str(visit.id)) is False
        assert mail.outbox == []
