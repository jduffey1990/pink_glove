"""
Small serializers shared across apps, used mainly to describe responses that
are plain dicts rather than model instances.

These exist for the OpenAPI schema (docs/DECISIONS.md ADR-019). A view that
returns `{"detail": "..."}` is perfectly correct without one, but the frontend
is written against generated types, so an undescribed response shape is a
response the frontend has to guess at.
"""

from rest_framework import serializers


class DetailSerializer(serializers.Serializer):
    """The `{"detail": "..."}` body DRF and this codebase use for messages."""

    detail = serializers.CharField()
