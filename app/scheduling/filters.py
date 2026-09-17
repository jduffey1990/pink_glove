"""
Job filtering.

`date_from` / `date_to` are **organization-local dates**, converted to a UTC
`scheduled_start` range here. The frontend never does timezone arithmetic: it
sends the date the dispatcher clicked on and gets that day as the organization
reckons it. A job at 23:30 on the 14th in Denver is the 15th in UTC, and it
still belongs under the 14th.
"""

import datetime as dt

from django_filters import rest_framework as filters
from rest_framework.exceptions import ValidationError

from scheduling.enums import JobStatus
from scheduling.models import Job, TimeEntry, assigned_to

#: Longest span a single query may cover. A dispatcher looks at a week, or at
#: most a month; an unbounded range over a busy tenant is a slow query nobody
#: asked for.
MAX_RANGE_DAYS = 62


def _local_day_bounds(organization, date_from, date_to):
    """The UTC half-open interval covering [date_from, date_to] locally."""
    tz = organization.tz
    start = end = None

    if date_from:
        start = dt.datetime.combine(date_from, dt.time.min, tzinfo=tz)
    if date_to:
        # Exclusive upper bound on the following midnight, which is cleaner
        # than time.max and does not lose the final microsecond of the day.
        end = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, tzinfo=tz)

    return start, end


class JobFilterSet(filters.FilterSet):
    date_from = filters.DateFilter(method="filter_noop")
    date_to = filters.DateFilter(method="filter_noop")
    status = filters.MultipleChoiceFilter(choices=JobStatus.choices)
    assignee = filters.UUIDFilter(method="filter_assignee")
    mine = filters.BooleanFilter(method="filter_mine")

    class Meta:
        model = Job
        fields = ["customer", "location", "plan", "status", "assignee", "mine"]

    def filter_noop(self, queryset, name, value):
        """
        Dates are applied together in `filter_queryset`, not one at a time --
        the range cap needs to see both ends before either is applied.
        """
        return queryset

    def filter_assignee(self, queryset, name, value):
        return queryset.filter(assigned_to(value)).distinct()

    def filter_mine(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(assigned_to(self.request.user)).distinct()

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)

        date_from = self.form.cleaned_data.get("date_from")
        date_to = self.form.cleaned_data.get("date_to")

        if date_from and date_to:
            if date_to < date_from:
                raise ValidationError({"date_to": "date_to cannot fall before date_from."})
            if (date_to - date_from).days > MAX_RANGE_DAYS:
                raise ValidationError(
                    {
                        "date_to": (
                            f"That range covers more than {MAX_RANGE_DAYS} days. "
                            "Ask for a shorter window."
                        )
                    }
                )

        organization = self.request.organization
        start, end = _local_day_bounds(organization, date_from, date_to)

        if start is not None:
            queryset = queryset.filter(scheduled_start__gte=start)
        if end is not None:
            queryset = queryset.filter(scheduled_start__lt=end)

        return queryset


class TimeEntryFilterSet(filters.FilterSet):
    date_from = filters.DateFilter(method="filter_noop")
    date_to = filters.DateFilter(method="filter_noop")

    class Meta:
        model = TimeEntry
        fields = ["job", "user"]

    def filter_noop(self, queryset, name, value):
        return queryset

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)

        date_from = self.form.cleaned_data.get("date_from")
        date_to = self.form.cleaned_data.get("date_to")
        start, end = _local_day_bounds(self.request.organization, date_from, date_to)

        if start is not None:
            queryset = queryset.filter(clock_in__gte=start)
        if end is not None:
            queryset = queryset.filter(clock_in__lt=end)

        return queryset
