"""
Recurrence expansion, written before the implementation it tests.

The whole reason `Organization.timezone` exists is the pair of DST tests
below. "Every Tuesday at 9am" is authored in local wall-clock time. Expand it
in UTC and every job silently shifts by an hour for half the year -- which
looks fine in a test that only ever runs in June, and puts a crew at the wrong
door in March.

So: build DTSTART naive, expand the rule naive, and only then attach the
organization's zone and convert to UTC.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from scheduling.services import expand_occurrences
from scheduling.tests.factories import RecurringPlanFactory

DENVER = ZoneInfo("America/Denver")


def _local_times(occurrences):
    """The occurrences as Denver wall-clock strings."""
    return [o.astimezone(DENVER).strftime("%Y-%m-%d %H:%M") for o in occurrences]


@pytest.mark.django_db
class TestDaylightSavingSpringForward:
    """
    2027-03-14 is the US spring-forward. A weekly Tuesday 9am plan spans it:
    2027-03-09 is MST (UTC-7), 2027-03-16 is MDT (UTC-6).
    """

    @pytest.fixture
    def plan(self, organization):
        return RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=WEEKLY;BYDAY=TU",
            starts_on=dt.date(2027, 3, 2),
            preferred_start_time=dt.time(9, 0),
        )

    def test_nine_am_stays_nine_am_across_the_transition(self, plan):
        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 3, 9), window_end=dt.date(2027, 3, 16)
        )

        assert _local_times(occurrences) == ["2027-03-09 09:00", "2027-03-16 09:00"]

    def test_the_underlying_utc_hour_shifts_by_one(self, plan):
        """
        The point of the exercise: same wall-clock time, different UTC instant.
        16:00Z before the change, 15:00Z after.
        """
        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 3, 9), window_end=dt.date(2027, 3, 16)
        )

        assert [o.hour for o in occurrences] == [16, 15]
        assert all(o.tzinfo == dt.UTC for o in occurrences)


@pytest.mark.django_db
class TestDaylightSavingFallBack:
    """2027-11-07 is the US fall-back, in the other direction."""

    @pytest.fixture
    def plan(self, organization):
        return RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=WEEKLY;BYDAY=TU",
            starts_on=dt.date(2027, 11, 1),
            preferred_start_time=dt.time(9, 0),
        )

    def test_nine_am_stays_nine_am_across_the_transition(self, plan):
        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 11, 2), window_end=dt.date(2027, 11, 9)
        )

        assert _local_times(occurrences) == ["2027-11-02 09:00", "2027-11-09 09:00"]

    def test_the_underlying_utc_hour_shifts_back(self, plan):
        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 11, 2), window_end=dt.date(2027, 11, 9)
        )

        assert [o.hour for o in occurrences] == [15, 16]


@pytest.mark.django_db
class TestNonexistentAndAmbiguousWallClockTimes:
    """
    02:30 does not exist on spring-forward day, and occurs twice on fall-back
    day. Python resolves both via `fold`, and the behaviour is asserted here
    rather than left to chance -- somebody will eventually schedule an
    overnight crew at 2am and deserve a documented answer.
    """

    def test_a_nonexistent_time_resolves_forward(self, organization):
        """
        2027-03-14 02:30 Denver never happens; the clock jumps 02:00 -> 03:00.
        With fold=0 the zone applies the pre-transition offset (UTC-7), so the
        instant lands at 09:30Z, which reads back as 03:30 local.
        """
        plan = RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=DAILY",
            starts_on=dt.date(2027, 3, 14),
            preferred_start_time=dt.time(2, 30),
        )

        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 3, 14), window_end=dt.date(2027, 3, 14)
        )

        assert len(occurrences) == 1
        assert occurrences[0] == dt.datetime(2027, 3, 14, 9, 30, tzinfo=dt.UTC)
        assert _local_times(occurrences) == ["2027-03-14 03:30"]

    def test_an_ambiguous_time_takes_the_first_pass(self, organization):
        """
        2027-11-07 01:30 Denver happens twice. fold=0 means the first (MDT,
        UTC-6) pass, so 07:30Z rather than 08:30Z.
        """
        plan = RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=DAILY",
            starts_on=dt.date(2027, 11, 7),
            preferred_start_time=dt.time(1, 30),
        )

        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 11, 7), window_end=dt.date(2027, 11, 7)
        )

        assert occurrences[0] == dt.datetime(2027, 11, 7, 7, 30, tzinfo=dt.UTC)


@pytest.mark.django_db
class TestExpansionWindow:
    @pytest.fixture
    def plan(self, organization):
        return RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=WEEKLY;BYDAY=TU",
            starts_on=dt.date(2027, 6, 1),
            preferred_start_time=dt.time(9, 0),
        )

    def test_both_window_ends_are_inclusive(self, plan):
        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 6, 1), window_end=dt.date(2027, 6, 15)
        )

        assert _local_times(occurrences) == [
            "2027-06-01 09:00",
            "2027-06-08 09:00",
            "2027-06-15 09:00",
        ]

    def test_nothing_before_starts_on_is_returned(self, plan):
        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 5, 1), window_end=dt.date(2027, 6, 8)
        )

        assert _local_times(occurrences) == ["2027-06-01 09:00", "2027-06-08 09:00"]

    def test_nothing_after_ends_on_is_returned(self, plan):
        plan.ends_on = dt.date(2027, 6, 8)
        plan.save()

        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 6, 1), window_end=dt.date(2027, 6, 30)
        )

        assert _local_times(occurrences) == ["2027-06-01 09:00", "2027-06-08 09:00"]

    def test_ends_on_is_inclusive_of_its_own_day(self, plan):
        plan.ends_on = dt.date(2027, 6, 8)
        plan.save()

        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 6, 8), window_end=dt.date(2027, 6, 30)
        )

        assert _local_times(occurrences) == ["2027-06-08 09:00"]

    def test_an_empty_window_returns_nothing(self, plan):
        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 6, 2), window_end=dt.date(2027, 6, 7)
        )

        assert occurrences == []

    def test_a_count_bounded_rule_stops_itself(self, organization):
        plan = RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=WEEKLY;BYDAY=TU;COUNT=3",
            starts_on=dt.date(2027, 6, 1),
            preferred_start_time=dt.time(9, 0),
        )

        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 6, 1), window_end=dt.date(2027, 12, 31)
        )

        assert len(occurrences) == 3

    def test_a_biweekly_rule_keeps_its_interval(self, organization):
        plan = RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=TU",
            starts_on=dt.date(2027, 6, 1),
            preferred_start_time=dt.time(9, 0),
        )

        occurrences = expand_occurrences(
            plan, window_start=dt.date(2027, 6, 1), window_end=dt.date(2027, 6, 30)
        )

        assert _local_times(occurrences) == [
            "2027-06-01 09:00",
            "2027-06-15 09:00",
            "2027-06-29 09:00",
        ]


@pytest.mark.django_db
class TestExpansionUsesTheOrganizationsOwnZone:
    def test_two_organizations_expand_the_same_rule_to_different_instants(
        self, organization, other_organization
    ):
        """Denver and New York both mean 9am locally, two hours apart in UTC."""
        common = {
            "rrule": "FREQ=WEEKLY;BYDAY=TU",
            "starts_on": dt.date(2027, 6, 1),
            "preferred_start_time": dt.time(9, 0),
        }
        denver_plan = RecurringPlanFactory(organization=organization, **common)
        ny_plan = RecurringPlanFactory(organization=other_organization, **common)

        window = {"window_start": dt.date(2027, 6, 1), "window_end": dt.date(2027, 6, 1)}
        denver = expand_occurrences(denver_plan, **window)
        new_york = expand_occurrences(ny_plan, **window)

        assert denver[0] == dt.datetime(2027, 6, 1, 15, 0, tzinfo=dt.UTC)
        assert new_york[0] == dt.datetime(2027, 6, 1, 13, 0, tzinfo=dt.UTC)
