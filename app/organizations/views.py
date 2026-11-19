from rest_framework.exceptions import NotFound
from rest_framework.generics import RetrieveUpdateAPIView

from base.permissions import IsAdminOrHigher, IsDispatcherOrHigher
from organizations.serializers import OrganizationSerializer


class CurrentOrganizationView(RetrieveUpdateAPIView):
    """
    The caller's own organization, for its settings page: dispatcher and
    above read it, admin and above change it. Cleaners and customers are
    refused and read the session's `OrganizationSummary` instead (ADR-040).

    Not a `TenantViewSetMixin` view: `Organization` is the tenant, not
    tenant-owned data. Scoping happens by only ever returning the organization
    the middleware resolved.
    """

    serializer_class = OrganizationSerializer

    def get_permissions(self):
        # The settings page's tier reads it; only admins may change it. Both
        # inherit `IsOrgMember`, so a deactivated organization stays readable.
        # Everything a cleaner's or customer's screen reads about the
        # organization (name, timezone, read-only, the notice written for
        # their role) is on the session summary; the tax rate, fees, invoice
        # terms, Stripe Connect and the subscription are the business's own.
        # The read tier is the `dispatcher` access of the settings route in
        # `ui/src/router/index.ts`, its one reader; move one and move both.
        permission_classes = (
            [IsDispatcherOrHigher] if self.request.method == "GET" else [IsAdminOrHigher]
        )
        return [permission() for permission in permission_classes]

    def get_object(self):
        organization = getattr(self.request, "organization", None)
        if organization is None:
            raise NotFound()
        return organization
