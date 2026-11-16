"""
Small serializers shared across apps, used mainly to describe responses that
are plain dicts rather than model instances.

These exist for the OpenAPI schema (docs/DECISIONS.md ADR-019). A view that
returns `{"detail": "..."}` is perfectly correct without one, but the frontend
is written against generated types, so an undescribed response shape is a
response the frontend has to guess at.
"""

from rest_framework import serializers

from base.validators import validate_date_range


class DetailSerializer(serializers.Serializer):
    """The `{"detail": "..."}` body DRF and this codebase use for messages."""

    detail = serializers.CharField()


class DateRangeQuerySerializer(serializers.Serializer):
    """
    `?date_from=&date_to=` for a report: both required, inclusive, and
    organization-local dates (invariant 8). The caller never sends an instant.

    Both are required, unlike the jobs filter's: a list without a range is a
    page of rows, a report without one is every row the tenant has ever
    written. A subclass sets `max_days` to the widest period its report is
    worth asking for.
    """

    max_days = 62

    date_from = serializers.DateField(
        help_text="First day of the period, as the organization reckons days. Inclusive."
    )
    date_to = serializers.DateField(
        help_text="Last day of the period, as the organization reckons days. Inclusive."
    )

    def validate(self, attrs):
        validate_date_range(attrs["date_from"], attrs["date_to"], max_days=self.max_days)
        return attrs
