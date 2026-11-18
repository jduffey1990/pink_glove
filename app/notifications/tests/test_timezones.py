"""
"Tomorrow" and "the reminder hour" are the organization's, not UTC's
(invariant 8).

Two ways to get it wrong, each pinned here: reckoning the day or the hour
in UTC, which picks the wrong visits for a tenant far from it; and treating
a day as 24 hours, which drops or doubles visits at the edges of a DST
change.
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
    """Auckland is UTC+13 in November."""

    #: 17:00 Wednesday 18 November in Auckland: 04:00 UTC, the same date.
    NOW = local(AUCKLAND, 2026, 11, 18, 17, 0)

    def test_the_hour_is_the_local_hour(self, auckland, make_visit):
        """At 04:00 UTC a UTC clock would still be waiting for 17:00."""
        make_visit(auckland, local(AUCKLAND, 2026, 11, 19, 10, 0))

        assert due(auckland, self.NOW - dt.timedelta(minutes=1)) == []
        assert len(due(auckland, self.NOW)) == 1

    def test_tomorrow_ends_at_the_local_midnight(self, auckland, make_visit):
        thursday = make_visit(auckland, local(AUCKLAND, 2026, 11, 19, 10, 0))
        # Friday 10:00 locally is Thursday 21:00 UTC: inside a UTC "tomorrow".
        make_visit(auckland, local(AUCKLAND, 2026, 11, 20, 10, 0))

        assert due(auckland, self.NOW) == [thursday]

    def test_in_the_morning_only_the_rest_of_the_local_today(self, auckland, make_visit):
        """
        10:00 Wednesday locally is 21:00 Tuesday UTC. A UTC reckoning is past
        its 17:00 and calls Wednesday "tomorrow", running until Thursday
        13:00 here, so it would remind Thursday's visit a day early.
        """
        morning = local(AUCKLAND, 2026, 11, 18, 10, 0)
        assert morning.astimezone(dt.UTC).date() == dt.date(2026, 11, 17)
        today = make_visit(auckland, local(AUCKLAND, 2026, 11, 18, 14, 0))
        make_visit(auckland, local(AUCKLAND, 2026, 11, 19, 10, 0))

        assert due(auckland, morning) == [today]


@pytest.fixture
def denver(db):
    return OrganizationFactory(timezone="America/Denver", visit_reminders_enabled=True)


@pytest.mark.django_db
class TestFallBack:
    """Sunday 1 November 2026 in Denver is 25 hours long: 01:00-02:00 happens twice."""

    NOW = local(DENVER, 2026, 10, 31, 17, 0)  # Saturday, MDT

    def test_the_window_ends_at_local_midnight_on_the_long_day(self, denver):
        _, end = reminder_window(denver, self.NOW)

        # Monday 00:00 MST, which a 24-hour day from Sunday 00:00 MDT misses by an hour.
        assert end.astimezone(dt.UTC) == dt.datetime(2026, 11, 2, 7, 0, tzinfo=dt.UTC)

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
        17:00 local is 23:00 UTC in October and 00:00 UTC in November. A fixed
        UTC hour would remind an hour early for half the year.
        """
        make_visit(denver, local(DENVER, 2026, 11, 3, 10, 0))
        monday_utc_2330 = dt.datetime(2026, 11, 2, 23, 30, tzinfo=dt.UTC)  # 16:30 MST

        assert due(denver, monday_utc_2330) == []
        assert len(due(denver, monday_utc_2330 + dt.timedelta(minutes=30))) == 1


@pytest.mark.django_db
class TestSpringForward:
    """Sunday 14 March 2027 in Denver is 23 hours long: 02:00-03:00 never happens."""

    NOW = local(DENVER, 2027, 3, 13, 17, 0)  # Saturday, MST

    def test_the_window_ends_at_local_midnight_on_the_short_day(self, denver):
        _, end = reminder_window(denver, self.NOW)

        assert end.astimezone(dt.UTC) == dt.datetime(2027, 3, 15, 6, 0, tzinfo=dt.UTC)

    def test_the_short_day_is_due_and_monday_midnight_is_not(self, denver, make_visit):
        early = make_visit(denver, local(DENVER, 2027, 3, 14, 0, 30))  # MST
        late = make_visit(denver, local(DENVER, 2027, 3, 14, 23, 30))  # MDT
        make_visit(denver, local(DENVER, 2027, 3, 15, 0, 0))

        assert due(denver, self.NOW) == [early, late]
