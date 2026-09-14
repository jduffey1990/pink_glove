"""
`Organization.is_within_business_hours` and the working-days validator.

The helper is what the access-reveal evaluator uses to judge a dispatcher's
reveal that has no job attached (ADR-017), so "was 10pm inside their day"
has to be answered in the organization's own timezone, not the server's.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError

from organizations.models import Organization, default_working_days, validate_working_days

DENVER = ZoneInfo("America/Denver")


def _denver(year, month, day, hour, minute=0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=DENVER)


@pytest.mark.django_db
class TestIsWithinBusinessHours:
    """Defaults: 07:00-19:00, Monday-Friday, America/Denver."""

    def test_midmorning_on_a_weekday_is_inside(self, organization):
        # Wednesday.
        assert organization.is_within_business_hours(_denver(2027, 3, 10, 10))

    def test_both_ends_are_inclusive(self, organization):
        assert organization.is_within_business_hours(_denver(2027, 3, 10, 7, 0))
        assert organization.is_within_business_hours(_denver(2027, 3, 10, 19, 0))

    def test_a_minute_either_side_is_outside(self, organization):
        assert not organization.is_within_business_hours(_denver(2027, 3, 10, 6, 59))
        assert not organization.is_within_business_hours(_denver(2027, 3, 10, 19, 1))

    def test_just_after_midnight_is_outside(self, organization):
        assert not organization.is_within_business_hours(_denver(2027, 3, 10, 0, 1))

    def test_just_before_midnight_is_outside(self, organization):
        assert not organization.is_within_business_hours(_denver(2027, 3, 10, 23, 59))

    def test_a_non_working_day_is_outside_at_any_hour(self, organization):
        # Saturday 2027-03-13, squarely inside the time window.
        assert not organization.is_within_business_hours(_denver(2027, 3, 13, 10))

    def test_the_judgement_is_made_in_the_organizations_timezone(self, organization):
        """
        03:00 UTC on a Thursday is 21:00 Wednesday in Denver -- outside. Read
        as UTC it would look like an ordinary weekday small hour, and read as
        server-local it would depend on where the server runs.
        """
        utc_3am = dt.datetime(2027, 3, 11, 3, 0, tzinfo=dt.UTC)

        assert utc_3am.astimezone(DENVER).hour == 20
        assert not organization.is_within_business_hours(utc_3am)

    def test_the_same_instant_can_differ_between_organizations(
        self, organization, other_organization
    ):
        """Denver and New York disagree about 20:30 UTC, which is the point."""
        instant = dt.datetime(2027, 3, 10, 20, 30, tzinfo=dt.UTC)

        assert organization.is_within_business_hours(instant)  # 13:30 Denver
        assert other_organization.is_within_business_hours(instant)  # 15:30 New York

        later = dt.datetime(2027, 3, 11, 1, 30, tzinfo=dt.UTC)

        assert organization.is_within_business_hours(later)  # 18:30 Denver
        assert not other_organization.is_within_business_hours(later)  # 20:30 New York

    def test_a_naive_datetime_is_taken_as_already_local(self, organization):
        assert organization.is_within_business_hours(dt.datetime(2027, 3, 10, 10, 0))
        assert not organization.is_within_business_hours(dt.datetime(2027, 3, 10, 22, 0))

    def test_an_overnight_window_wraps_past_midnight(self, organization):
        organization.business_hours_start = dt.time(20, 0)
        organization.business_hours_end = dt.time(4, 0)
        organization.save()

        assert organization.is_within_business_hours(_denver(2027, 3, 10, 22))
        assert organization.is_within_business_hours(_denver(2027, 3, 10, 2))
        assert not organization.is_within_business_hours(_denver(2027, 3, 10, 12))

    def test_a_custom_working_week_is_honoured(self, organization):
        organization.working_days = [6, 7]  # weekends only
        organization.save()

        assert not organization.is_within_business_hours(_denver(2027, 3, 10, 10))
        assert organization.is_within_business_hours(_denver(2027, 3, 13, 10))


@pytest.mark.django_db
class TestDefaults:
    def test_a_new_organization_works_weekdays(self, organization):
        assert organization.working_days == [1, 2, 3, 4, 5]

    def test_the_default_is_not_shared_between_instances(self):
        """A mutable default would be; `default_working_days` is a callable."""
        first = Organization.objects.create(name="First")
        second = Organization.objects.create(name="Second")

        first.working_days.append(6)

        assert second.working_days == default_working_days()

    def test_reveal_buffers_default_wider_after_than_before(self, organization):
        assert organization.reveal_buffer_before_minutes == 60
        assert organization.reveal_buffer_after_minutes == 120


class TestValidateWorkingDays:
    @pytest.mark.parametrize("value", [[1], [1, 2, 3, 4, 5], [7], [1, 7]])
    def test_valid_values_pass(self, value):
        validate_working_days(value)

    @pytest.mark.parametrize(
        "value",
        [
            [0],  # 0 is Monday in some conventions, not this one
            [8],
            [-1],
            [1, 1],  # repeats
            ["1"],  # string, not int
            [True],  # bool is an int subclass and would mean Monday
            "12345",  # not a list
            {1: 2},
        ],
    )
    def test_invalid_values_are_rejected(self, value):
        with pytest.raises(ValidationError):
            validate_working_days(value)
