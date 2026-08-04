from django.db.models import TextChoices


class Role(TextChoices):
    """
    A user's role *within one organization*. Carried on `Membership`
    (Phase 1), not on the user -- the same person may hold different roles in
    different organizations.
    """

    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    DISPATCHER = "dispatcher", "Dispatcher"
    CLEANER = "cleaner", "Cleaner"
    CUSTOMER = "customer", "Customer"


#: Escalating privilege tiers, most privileged first. `base.permissions`
#: builds its permission classes from these.
OWNER_ROLES = (Role.OWNER,)
ADMIN_ROLES = (*OWNER_ROLES, Role.ADMIN)
DISPATCHER_ROLES = (*ADMIN_ROLES, Role.DISPATCHER)
STAFF_ROLES = (*DISPATCHER_ROLES, Role.CLEANER)

#: Roles that must clear a 2FA challenge on every login. Cleaners are
#: challenged only on an untrusted device; customers use magic links.
ALWAYS_TWO_FACTOR_ROLES = DISPATCHER_ROLES
