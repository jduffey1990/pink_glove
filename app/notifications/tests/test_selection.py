"""
Which visits are due a reminder (`notifications.services.due_reminders`).

One test per condition, each against a visit that would otherwise be due,
so a condition that stops filtering fails its own test and no other.
"""

import datetime as dt

import pytest

from customers.enums import CustomerStatus
from notifications.models import VisitReminder
from notifications.services import due_reminders
from notifications.tests.clock import DENVER, NOW, TOMORROW_2PM, local
from scheduling.enums import JobStatus
from scheduling.tests.factories import OrganizationFactory


def due(organization, now=NOW):
    return list(due_reminders(organization, now))


@pytest.mark.django_db
class TestWhatIsDue:
    def test_a_scheduled_visit_tomorrow_is_due(self, org, make_visit):
        visit = make_visit(org, TOMORROW_2PM)

        assert due(org) == [visit]

    @pytest.mark.parametrize("status", [CustomerStatus.LEAD, CustomerStatus.ACTIVE])
    def test_for_a_lead_or_an_active_customer(self, org, make_visit, status):
        visit = make_visit(org, TOMORROW_2PM, customer__status=status)

        assert due(org) == [visit]

    def test_from_the_first_minute_of_tomorrow_to_the_last(self, org, make_visit):
        first = make_visit(org, local(DENVER, 2026, 11, 19, 0, 0))
        last = make_visit(org, local(DENVER, 2026, 11, 19, 23, 59))

        assert due(org) == [first, last]

    def test_a_visit_later_today_is_still_due(self, org, make_visit):
        """Its day-before 17:00 is long past and it has not started."""
        later = make_visit(org, local(DENVER, 2026, 11, 18, 19, 0))

        assert due(org) == [later]

    def test_not_a_visit_that_has_started(self, org, make_visit):
        make_visit(org, local(DENVER, 2026, 11, 18, 17, 30))
        make_visit(org, NOW)

        assert due(org) == []

    def test_not_a_visit_the_day_after_tomorrow(self, org, make_visit):
        make_visit(org, local(DENVER, 2026, 11, 20, 0, 0))

        assert due(org) == []

    def test_tomorrow_waits_for_the_reminder_hour(self, org, make_visit):
        make_visit(org, TOMORROW_2PM)

        assert due(org, local(DENVER, 2026, 11, 18, 16, 59)) == []
        assert len(due(org, local(DENVER, 2026, 11, 18, 17, 0))) == 1

    def test_before_the_hour_the_rest_of_today_is_due(self, org, make_visit):
        """A visit booked at 08:00 for 15:00 the same day is reminded at once."""
        this_afternoon = make_visit(org, local(DENVER, 2026, 11, 18, 15, 0))
        make_visit(org, TOMORROW_2PM)

        assert due(org, local(DENVER, 2026, 11, 18, 8, 0)) == [this_afternoon]

    def test_still_due_late_in_the_evening(self, org, make_visit):
        make_visit(org, TOMORROW_2PM)

        assert len(due(org, local(DENVER, 2026, 11, 18, 23, 30))) == 1

    def test_still_due_after_midnight_if_it_was_missed(self, org, make_visit):
        """Booked after the last tick before midnight: the next tick still catches it."""
        visit = make_visit(org, local(DENVER, 2026, 11, 19, 8, 0))

        assert due(org, local(DENVER, 2026, 11, 19, 0, 15)) == [visit]


@pytest.mark.django_db
class TestWhatIsSkipped:
    @pytest.mark.parametrize("status", [s for s in JobStatus if s != JobStatus.SCHEDULED])
    def test_a_visit_not_waiting_to_happen(self, org, make_visit, status):
        """Terminal ones are over or called off; en route or in progress, it is under way."""
        make_visit(org, TOMORROW_2PM, status=status)

        assert due(org) == []

    def test_a_deleted_visit(self, org, make_visit):
        make_visit(org, TOMORROW_2PM).delete()

        assert due(org) == []

    def test_a_deleted_customer(self, org, make_visit):
        make_visit(org, TOMORROW_2PM).customer.delete()

        assert due(org) == []

    def test_a_deleted_location(self, org, make_visit):
        make_visit(org, TOMORROW_2PM).location.delete()

        assert due(org) == []

    def test_a_customer_with_no_email(self, org, make_visit):
        make_visit(org, TOMORROW_2PM, customer__email="")

        assert due(org) == []

    @pytest.mark.parametrize("status", [CustomerStatus.INACTIVE, CustomerStatus.ARCHIVED])
    def test_an_inactive_or_archived_customer(self, org, make_visit, status):
        make_visit(org, TOMORROW_2PM, customer__status=status)

        assert due(org) == []

    def test_an_organization_that_has_not_turned_them_on(self, org, make_visit):
        make_visit(org, TOMORROW_2PM)
        org.visit_reminders_enabled = False

        assert due(org) == []

    def test_a_deactivated_organization(self, org, make_visit):
        """Read-only (ADR-028): it tells its customers nothing until it is back."""
        make_visit(org, TOMORROW_2PM)
        org.is_active = False

        assert due(org) == []

    def test_a_visit_already_reminded_at_this_time(self, org, make_visit):
        visit = make_visit(org, TOMORROW_2PM)
        VisitReminder.objects.create(
            organization=org,
            job=visit,
            scheduled_start=visit.scheduled_start,
            recipient=visit.customer.email,
            sequence=1,
        )

        assert due(org) == []


@pytest.mark.django_db
class TestRescheduled:
    def test_a_visit_moved_after_its_reminder_is_due_again(self, org, make_visit):
        """The customer was told the old time; the new one needs telling."""
        visit = make_visit(org, TOMORROW_2PM)
        VisitReminder.objects.create(
            organization=org,
            job=visit,
            scheduled_start=visit.scheduled_start,
            recipient=visit.customer.email,
            sequence=1,
        )

        visit.scheduled_start += dt.timedelta(hours=1)
        visit.scheduled_end += dt.timedelta(hours=1)
        visit.save()

        assert due(org) == [visit]

    @pytest.mark.parametrize(
        ("current", "is_due"), [(TOMORROW_2PM, True), (TOMORROW_2PM + dt.timedelta(hours=2), False)]
    )
    def test_only_the_latest_reminder_counts(self, org, make_visit, current, is_due):
        """Reminded at 2:00, then at 4:00: back at 2:00 is due, still at 4:00 is not."""
        visit = make_visit(org, current)
        for sequence, start in enumerate(
            [TOMORROW_2PM, TOMORROW_2PM + dt.timedelta(hours=2)], start=1
        ):
            VisitReminder.objects.create(
                organization=org,
                job=visit,
                scheduled_start=start,
                recipient=visit.customer.email,
                sequence=sequence,
            )

        assert due(org) == ([visit] if is_due else [])

    def test_a_reminder_for_a_withdrawn_claim_does_not_count(self, org, make_visit):
        """A soft-deleted ledger row is not a reminder anyone received."""
        visit = make_visit(org, TOMORROW_2PM)
        VisitReminder.objects.create(
            organization=org,
            job=visit,
            scheduled_start=visit.scheduled_start,
            recipient=visit.customer.email,
            sequence=1,
        ).delete()

        assert due(org) == [visit]


@pytest.mark.django_db
class TestTenancy:
    def test_only_the_organizations_own_visits(self, org, make_visit):
        rival = OrganizationFactory(timezone="America/Denver", visit_reminders_enabled=True)
        ours = make_visit(org, TOMORROW_2PM)
        theirs = make_visit(rival, TOMORROW_2PM)

        assert due(org) == [ours]
        assert due(rival) == [theirs]
