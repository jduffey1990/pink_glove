"""
Role-based permission classes.

Roles are read from `request.membership`, which `TenantMiddleware` resolved --
so these cost no extra queries.

A superuser passes any role check, but only once an organization has been
resolved (via the X-Organization header). That keeps platform administration
possible without letting it bypass tenant scoping: the queryset filter in
`TenantViewSetMixin` still applies.
"""

from rest_framework.permissions import BasePermission

from users.enums import ADMIN_ROLES, DISPATCHER_ROLES, OWNER_ROLES, STAFF_ROLES, Role


class IsOrgMember(BasePermission):
    """Authenticated, and acting within a resolved organization."""

    message = "You must be acting within an organization to do that."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if getattr(request, "organization", None) is None:
            return False
        return user.is_superuser or getattr(request, "membership", None) is not None


class _RoleRequired(IsOrgMember):
    allowed_roles: tuple[str, ...] = ()

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.user.is_superuser:
            return True
        return request.membership.role in self.allowed_roles


class IsOwner(_RoleRequired):
    allowed_roles = OWNER_ROLES
    message = "Only the organization owner can do that."


class IsAdminOrHigher(_RoleRequired):
    allowed_roles = ADMIN_ROLES
    message = "You need admin access to do that."


class IsDispatcherOrHigher(_RoleRequired):
    allowed_roles = DISPATCHER_ROLES
    message = "You need dispatcher access to do that."


class IsStaff(_RoleRequired):
    """Any internal role. Excludes customers."""

    allowed_roles = STAFF_ROLES
    message = "This is only available to organization staff."


class IsCustomer(_RoleRequired):
    allowed_roles = (Role.CUSTOMER,)
    message = "This is only available to customers."


class IsSelfOrAdminOrHigher(IsOrgMember):
    """
    Object-level: the caller is the user in question, or an admin/owner.

    Expects the object to be a user, or to expose a `user` attribute.
    """

    message = "You can only act on your own record."

    def has_object_permission(self, request, view, obj):
        if request.user.is_superuser:
            return True

        target_user_id = getattr(obj, "user_id", None) or getattr(obj, "id", None)
        if target_user_id == request.user.id:
            return True

        membership = getattr(request, "membership", None)
        return membership is not None and membership.role in ADMIN_ROLES
