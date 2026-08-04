"""
The tenant scoping chokepoint.

Every view over a `TenantModel` inherits `TenantViewSetMixin`. This is the one
place tenant filtering happens, and `base/tests/test_tenancy.py` fails CI if a
view slips past it.

Do not replace this with a manager that auto-filters from request context --
see docs/DECISIONS.md ADR-002 for what that breaks.
"""

from rest_framework import serializers
from rest_framework.exceptions import NotFound


class TenantViewSetMixin:
    """
    Scopes reads to the caller's organization and stamps writes with it.

    `organization` is taken from the resolved tenant and never from request
    data, so a forged `organization` field in a payload has no effect.
    """

    def get_organization(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            # 404 rather than 403: a 403 would confirm the resource exists.
            raise NotFound()
        return organization

    def get_queryset(self):
        return super().get_queryset().filter(organization=self.get_organization())

    def perform_create(self, serializer):
        serializer.save(organization=self.get_organization())

    def perform_update(self, serializer):
        # Passed on update too, so a record can never be moved between
        # organizations by a crafted payload.
        serializer.save(organization=self.get_organization())


class TenantModelSerializer(serializers.ModelSerializer):
    """
    Base serializer for tenant-owned models.

    `organization` is read-only: the viewset supplies it. Belt and braces
    alongside `TenantViewSetMixin.perform_create`.
    """

    class Meta:
        read_only_fields = ("id", "organization", "created_at", "updated_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "organization" in self.fields:
            self.fields["organization"].read_only = True
