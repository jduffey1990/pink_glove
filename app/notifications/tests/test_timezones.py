"""
"Tomorrow" and "the reminder hour" are the organization's, not UTC's
(invariant 8).

Two ways to get it wrong, each pinned here: reckoning the day in UTC, which
picks the wrong day for a tenant far from it; and treating a day as 24
hours, which drops or doubles visits at the edges of a DST change.
"""

import datetime as dt

import pytest

from notifications.services import due_reminders, reminder_window
from notifications.tests.clock import AUCKLAND, DENVER, local
from scheduling.tests.factories import OrganizationFactory


def due(organization, now):
    return list(due_reminders(organization, now))


@pytest.fixture
def auckland(db):
    return OrganizationFactory(timezone="Pacific/Auckland", visit_reminders_enabled=True)


@pytest.mark.django_db
class TestFarFromUtc:
    """Auckland is UTC+13 in November: its tomorrow is UTC's day after tomorrow."""

    #: 09:00 Wednesday 18 November in Auckland, which is still Tuesday in UTC.
    NOW = local(AUCKLAND, 2026, 11, 18, 9, 0)

    def test_the_utc_date_is_a_day_behind(self):
        assert self.NOW.astimezone(dt.UTC).date() == dt.date(2026, 11, 17)

    def test_tomorrow_is_the_local_tomorrow(self, auckland, make_visit):
        thursday = make_visit(auckland, local(AUCKLAND, 2026, 11, 19, 10, 0))
        # Wednesday the 18th locally: what a UTC reckoning would call tomorrow.
        make_visit(auckland, local(AUCKLAND, 2026, 11, 18, 14, 0))

        assert due(auckland, self.NOW) == [thursday]

    def test_the_hour_is_the_local_hour(self, auckland, make_visit):
        make_visit(auckland, local(AUCKLAND, 2026, 11, 19, 10, 0))

        assert due(auckland, self.NOW - dt.timedelta(minutes=1)) == []
        assert len(due(auckland, self.NOW)) == 1


@pytest.fixture
def denver(db):
    return OrganizationFactory(timezone="America/Denver", visit_reminders_enabled=True)


@pytest.mark.django_db
class TestFallBack:
    """Sunday 1 November 2026 in Denver is 25 hours long: 01:00-02:00 happens twice."""

    NOW = local(DENVER, 2026, 10, 31, 9, 0)  # Saturday, MDT

    def test_the_window_is_twenty_five_hours(self, denver):
        start, end = reminder_window(denver, self.NOW)

        # In UTC: Python subtracts two times in one zone by the wall clock.
        assert end.astimezone(dt.UTC) - start.astimezone(dt.UTC) == dt.timedelta(hours=25)

    def test_the_whole_long_day_is_due_and_nothing_after(self, denver, make_visit):
        early = make_visit(denver, local(DENVER, 2026, 11, 1, 0, 30))  # still MDT
        late = make_visit(denver, local(DENVER, 2026, 11, 1, 23, 30))  # MST
        make_visit(denver, local(DENVER, 2026, 11, 2, 0, 30))

        assert due(denver, self.NOW) == [early, late]

    def test_a_visit_in_the_late_hour_is_due_although_it_is_monday_in_utc(self, denver, make_visit):
        late = make_visit(denver, local(DENVER, 2026, 11, 1, 23, 30))

        assert late.scheduled_start.astimezone(dt.UTC).date() == dt.date(2026, 11, 2)
        assert due(denver, self.NOW) == [late]

    def test_the_hour_moves_with_the_clocks(self, denver, make_visit):
        """
        09:00 local is 15:00 UTC in October and 16:00 UTC in November. A fixed
        UTC hour would remind an hour early for half the year.
        """
        make_visit(denver, local(DENVER, 2026, 11, 3, 10, 0))
        monday_utc_1530 = dt.datetime(2026, 11, 2, 15, 30, tzinfo=dt.UTC)  # 08:30 MST

        assert due(denver, monday_utc_1530) == []
        assert len(due(denver, monday_utc_1530 + dt.timedelta(minutes=30))) == 1


@pytest.mark.django_db
class TestSpringForward:
    """Sunday 14 March 2027 in Denver is 23 hours long: 02:00-03:00 never happens."""

    NOW = local(DENVER, 2027, 3, 13, 9, 0)  # Saturday, MST

    def test_the_window_is_twenty_three_hours(self, denver):
        start, end = reminder_window(denver, self.NOW)

        # In UTC: Python subtracts two times in one zone by the wall clock.
        assert end.astimezone(dt.UTC) - start.astimezone(dt.UTC) == dt.timedelta(hours=23)

    def test_the_short_day_is_due_and_monday_midnight_is_not(self, denver, make_visit):
        early = make_visit(denver, local(DENVER, 2027, 3, 14, 0, 30))  # MST
        late = make_visit(denver, local(DENVER, 2027, 3, 14, 23, 30))  # MDT
        make_visit(denver, local(DENVER, 2027, 3, 15, 0, 0))

        assert due(denver, self.NOW) == [early, late]
