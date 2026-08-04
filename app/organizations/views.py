from rest_framework.exceptions import NotFound
from rest_framework.generics import RetrieveUpdateAPIView

from base.permissions import IsAdminOrHigher, IsOrgMember
from organizations.serializers import OrganizationSerializer


class CurrentOrganizationView(RetrieveUpdateAPIView):
    """
    The caller's own organization.

    Not a `TenantViewSetMixin` view: `Organization` is the tenant, not
    tenant-owned data. Scoping happens by only ever returning the organization
    the middleware resolved.
    """

    serializer_class = OrganizationSerializer

    def get_permissions(self):
        # Any member may read their organization; only admins may change it.
        permission_classes = [IsOrgMember] if self.request.method == "GET" else [IsAdminOrHigher]
        return [permission() for permission in permission_classes]

    def get_object(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            raise NotFound()
        return organization
