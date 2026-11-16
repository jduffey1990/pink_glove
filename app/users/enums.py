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

#: The roles the team page can hand out, as a choice set: every role but
#: customer. Named in ENUM_NAME_OVERRIDES so the schema calls it one thing.
STAFF_ROLE_CHOICES = [(role.value, role.label) for role in STAFF_ROLES]

#: Roles that must clear a 2FA challenge on every login. Cleaners are
#: challenged only on an untrusted device; customers use magic links.
ALWAYS_TWO_FACTOR_ROLES = DISPATCHER_ROLES

#: Who may manage whom on the team (ADR-028): the roles each tier may invite,
#: grant, and act on. An owner manages anyone; an admin manages the tiers
#: below admin and nothing else; nobody below admin manages anybody. Customer
#: is in no list: portal access is not handed out from the team page.
MANAGEABLE_ROLES = {
    Role.OWNER: STAFF_ROLES,
    Role.ADMIN: (Role.DISPATCHER, Role.CLEANER),
}


class MembershipState(TextChoices):
    """
    Where a membership stands, derived from its columns and never stored.

    `is_active` alone cannot say it: an invitation nobody has accepted and a
    person who was switched off are both inactive, and the team page has to
    offer "resend" for one and "reactivate" for the other.
    """

    INVITED = "invited", "Invited"
    ACTIVE = "active", "Active"
    DEACTIVATED = "deactivated", "Deactivated"


class TeamAction(TextChoices):
    """
    What may be done to a membership from the team page.

    Published per row as `available_actions`, already worked out for the
    caller, so the page draws its buttons from the answer instead of keeping
    its own copy of who outranks whom (ADR-023, ADR-028).
    """

    CHANGE_ROLE = "change_role", "Change role"
    DEACTIVATE = "deactivate", "Deactivate"
    REACTIVATE = "reactivate", "Reactivate"
    RESEND_INVITE = "resend_invite", "Resend invitation"


class AccountTokenPurpose(TextChoices):
    """What an emailed account link is good for. One purpose, one endpoint."""

    INVITE = "invite", "Invitation"
    PASSWORD_RESET = "password_reset", "Password reset"
