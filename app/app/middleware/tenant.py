"""
Resolves the organization for the current request.

Sets two attributes on every request:
    request.organization -> Organization | None
    request.membership   -> Membership | None  (the caller's role in it)

Resolution order:
    1. Anonymous                     -> None; permission classes reject
    2. Exactly one active membership -> that organization
    3. Several memberships           -> X-Organization header, validated
                                        against the caller's own memberships
    4. Superuser                     -> X-Organization for any organization

Background work never consults this. Celery tasks take `organization_id` as an
explicit argument -- in a worker there is no request, so an implicit lookup
would resolve to None and fail silently. See docs/DECISIONS.md ADR-004.
"""

import uuid

ORGANIZATION_HEADER = "X-Organization"


def _parse_uuid(raw: str | None) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


class TenantMiddleware:
    """Must be ordered after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        membership, organization = self._resolve(request)
        request.membership = membership
        request.organization = organization
        return self.get_response(request)

    def _resolve(self, request):
        from organizations.models import Organization
        from users.models import Membership

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None, None

        requested_id = _parse_uuid(request.headers.get(ORGANIZATION_HEADER))

        memberships = Membership.objects.filter(user=user, is_active=True).select_related(
            "organization"
        )

        if requested_id is not None:
            membership = memberships.filter(organization_id=requested_id).first()
            if membership is not None:
                return membership, membership.organization
            # A superuser may act on any organization; anyone else asking for
            # one they don't belong to gets nothing.
            if user.is_superuser:
                return None, Organization.objects.filter(pk=requested_id).first()
            return None, None

        # Slice to 2: we only need to know "exactly one" vs "more than one".
        candidates = list(memberships[:2])
        if len(candidates) == 1:
            return candidates[0], candidates[0].organization

        # Zero memberships, or several with no header to disambiguate.
        return None, None
