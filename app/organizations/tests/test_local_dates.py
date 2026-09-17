"""
`Organization.today()` and `Organization.local_day_bounds()`.

Both answer "what does this business call this day", which is a different
question from what UTC calls it. The schedule, the audit evaluator and billing
all ask it, so there is one answer rather than three.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from organizations.models import Organization

DENVER = ZoneInfo("America/Denver")


def _elapsed(start: dt.datetime, end: dt.datetime) -> dt.timedelta:
    """Real elapsed time. See `test_a_spring_forward_day_is_23_hours_long`."""
    return end.astimezone(dt.UTC) - start.astimezone(dt.UTC)


@pytest.fixture
def denver(db):
    return Organization.objects.create(name="Mile High Clean", timezone="America/Denver")


@pytest.fixture
def auckland(db):
    return Organization.objects.create(name="Southerly Clean", timezone="Pacific/Auckland")


@pytest.mark.django_db
class TestToday:
    def test_is_the_local_date_not_the_utc_one(self, denver, auckland):
        # 04:00 UTC on the 15th: still the 14th in Denver, already the 15th in
        # Auckland. UTC's own answer is right for neither.
        moment = dt.datetime(2027, 6, 15, 4, 0, tzinfo=dt.UTC)
        with timezone.override(None):
            from unittest import mock

            with mock.patch("django.utils.timezone.now", return_value=moment):
                assert denver.today() == dt.date(2027, 6, 14)
                assert auckland.today() == dt.date(2027, 6, 15)

    def test_is_a_date(self, denver):
        assert isinstance(denver.today(), dt.date)


@pytest.mark.django_db
class TestLocalDayBounds:
    def test_a_single_day_starts_at_local_midnight(self, denver):
        start, end = denver.local_day_bounds(dt.date(2027, 6, 14), dt.date(2027, 6, 14))

        assert start == dt.datetime(2027, 6, 14, 0, 0, tzinfo=DENVER)
        # Half-open on the following midnight, so 23:59:59.999999 is included
        # and the next day is not.
        assert end == dt.datetime(2027, 6, 15, 0, 0, tzinfo=DENVER)

    def test_the_last_microsecond_of_the_day_falls_inside(self, denver):
        start, end = denver.local_day_bounds(dt.date(2027, 6, 14), dt.date(2027, 6, 14))
        last = dt.datetime(2027, 6, 14, 23, 59, 59, 999999, tzinfo=DENVER)

        assert start <= last < end

    def test_each_end_is_optional(self, denver):
        assert denver.local_day_bounds() == (None, None)

        start, end = denver.local_day_bounds(dt.date(2027, 6, 14))
        assert start is not None and end is None

        start, end = denver.local_day_bounds(None, dt.date(2027, 6, 14))
        assert start is None and end is not None

    def test_the_bounds_follow_the_organization_timezone(self, denver, auckland):
        day = dt.date(2027, 6, 14)

        denver_start, _ = denver.local_day_bounds(day, day)
        auckland_start, _ = auckland.local_day_bounds(day, day)

        # Auckland's day opens well before Denver's, in UTC terms.
        assert auckland_start < denver_start

    def test_a_spring_forward_day_is_23_hours_long(self, denver):
        # 2027-03-14: Denver loses an hour at 02:00 local. Compared in UTC on
        # purpose -- subtracting two datetimes that share a tzinfo object
        # ignores the offset and would answer 24 for every day of the year.
        start, end = denver.local_day_bounds(dt.date(2027, 3, 14), dt.date(2027, 3, 14))

        assert _elapsed(start, end) == dt.timedelta(hours=23)

    def test_a_fall_back_day_is_25_hours_long(self, denver):
        # 2027-11-07: Denver gains an hour at 02:00 local.
        start, end = denver.local_day_bounds(dt.date(2027, 11, 7), dt.date(2027, 11, 7))

        assert _elapsed(start, end) == dt.timedelta(hours=25)
