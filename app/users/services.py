"""
Sign-in rules that more than one view has to agree on, and the rules of the
team page: who may manage whom, and what an invitation does (ADR-028).
"""

import logging
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import EmailMessage, send_mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from app.exceptions import BadRequestJSON, ConflictError
from users.enums import (
    MANAGEABLE_ROLES,
    STAFF_ROLES,
    AccountTokenPurpose,
    MembershipState,
    Role,
    TeamAction,
)
from users.models import AccountToken, CustomUser, MagicLinkToken, Membership

logger = logging.getLogger(__name__)

#: One sentence for every way an invitation link can be no good: unknown,
#: expired, used, withdrawn, or meant for something else. Saying which would
#: tell a stranger with a guess what they are holding.
INVALID_INVITATION = "That invitation link is invalid or has expired. Ask for a new one."

ALREADY_ON_THE_TEAM = (
    "That person already belongs to this organization. "
    "If they were deactivated, reactivate them instead."
)

LAST_OWNER = "An organization must keep at least one active owner."


def may_use_magic_link(user: CustomUser) -> bool:
    """
    Magic links are the *customer* flow and nobody else's (ADR-008).

    A link is a single factor: whoever can read the mailbox is signed in. That
    is the right trade for a homeowner checking an invoice, and it would
    quietly replace password + 2FA for an owner if staff could use it too. So
    the test is "is a customer and nothing more" -- one staff membership
    anywhere, or none at all, and the answer is no.
    """
    if user.is_superuser or user.is_staff:
        return False

    roles = set(Membership.objects.filter(user=user, is_active=True).values_list("role", flat=True))
    return roles == {Role.CUSTOMER}


def may_reset_password(user: CustomUser) -> bool:
    """
    Password reset is the *staff* flow, the mirror of the rule above.

    A customer has no password to reset and signs in by magic link. A platform
    superuser is left out on purpose: that account can act in every
    organization, and its password is changed from a shell
    (`manage.py changepassword`), not by whoever can read a mailbox.
    """
    if not user.is_active or user.deleted_at is not None:
        return False
    if user.is_superuser or user.is_staff:
        return False

    return Membership.objects.filter(user=user, is_active=True, role__in=STAFF_ROLES).exists()


def send_magic_link(user: CustomUser, link: str) -> None:
    """Email the link. A mail failure is logged, never surfaced -- see the view."""
    ttl_minutes = int(MagicLinkToken.TTL.total_seconds() // 60)

    try:
        send_mail(
            subject="Your sign-in link",
            message=f"Sign in here: {link}\n\nThis link expires in {ttl_minutes} minutes.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send magic link to user %s", user.pk)


def validated_password(password: str, user: CustomUser) -> str:
    """
    Run Django's password validators, reporting a refusal as a field error.

    The one way a password is accepted from the public: the invitation and
    the reset both come through here, so neither can end up with a weaker
    rule than the other.
    """
    try:
        validate_password(password, user=user)
    except DjangoValidationError as error:
        raise ValidationError({"password": list(error.messages)}) from error
    return password


# --------------------------------------------------------------------------
# The team page: who may manage whom
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Who is managing the team: the caller, and the membership they act through."""

    user: CustomUser
    #: None for a superuser acting on an organization by header.
    membership: Membership | None

    @classmethod
    def of(cls, request) -> "Actor":
        return cls(user=request.user, membership=getattr(request, "membership", None))

    @property
    def manageable_roles(self) -> tuple[Role, ...]:
        """The roles this caller may invite, grant, and act on."""
        if self.user.is_superuser:
            return STAFF_ROLES
        if self.membership is None:
            return ()
        return MANAGEABLE_ROLES.get(self.membership.role, ())

    def is_self(self, target: Membership) -> bool:
        return target.user_id == self.user.id


@dataclass(frozen=True)
class _Refusal:
    detail: str
    #: True for "you may not" (403); False for "not in its current state" (409).
    forbidden: bool


def _active_owners(organization):
    """
    Owners who can actually sign in and act.

    `user__deleted_at` is filtered by hand: the manager only hides soft-deleted
    memberships, not a membership whose user was soft-deleted.
    """
    return Membership.objects.filter(
        organization=organization,
        role=Role.OWNER,
        is_active=True,
        user__is_active=True,
        user__deleted_at__isnull=True,
    )


def sole_active_owner_id(organization) -> uuid.UUID | None:
    """The membership id of the only active owner, or None when there are more (or none)."""
    ids = list(_active_owners(organization).values_list("id", flat=True)[:2])
    return ids[0] if len(ids) == 1 else None


def _not_manageable(actor: Actor, role: str) -> str | None:
    """Why this caller may not manage or grant `role`, or None if they may."""
    if role == Role.CUSTOMER:
        return "Customer access is not managed from the team page."
    if role in actor.manageable_roles:
        return None
    if actor.manageable_roles:
        return "Only an owner can manage owners and admins."
    return "You need admin access to manage the team."


def _refusal(
    actor: Actor, target: Membership, action: TeamAction, sole_owner_id: uuid.UUID | None
) -> _Refusal | None:
    """
    Why `actor` may not do `action` to `target`, or None if they may.

    The one statement of the team rules. `available_actions` publishes its
    answers and every write below enforces them, so what a row offers and
    what the server accepts cannot drift apart.
    """
    not_manageable = _not_manageable(actor, target.role)
    if not_manageable:
        return _Refusal(not_manageable, forbidden=True)

    state = target.state

    if action == TeamAction.REACTIVATE:
        if state != MembershipState.DEACTIVATED:
            return _Refusal("That person is not deactivated.", forbidden=False)
        return None

    if action == TeamAction.RESEND_INVITE:
        if state != MembershipState.INVITED:
            return _Refusal("There is no pending invitation to resend.", forbidden=False)
        return None

    # Changing a role and deactivating are the two that can lock someone out.
    if actor.is_self(target):
        if action == TeamAction.CHANGE_ROLE:
            return _Refusal("You cannot change your own role.", forbidden=True)
        return _Refusal("You cannot deactivate yourself.", forbidden=True)

    if state == MembershipState.DEACTIVATED:
        if action == TeamAction.CHANGE_ROLE:
            return _Refusal("Reactivate this person before changing their role.", forbidden=False)
        return _Refusal("That person is already deactivated.", forbidden=False)

    # Any role change open to an owner's row is a demotion, so the last owner
    # keeps both the role and the membership.
    if target.id == sole_owner_id:
        return _Refusal(LAST_OWNER, forbidden=False)

    return None


def available_actions(
    actor: Actor, target: Membership, sole_owner_id: uuid.UUID | None
) -> list[str]:
    """What `actor` may do to `target` right now. Published per row."""
    return [
        action.value
        for action in TeamAction
        if _refusal(actor, target, action, sole_owner_id) is None
    ]


def assignable_roles(
    actor: Actor, target: Membership, sole_owner_id: uuid.UUID | None
) -> list[str]:
    """The roles `actor` may move `target` to. Empty when they may not at all."""
    if _refusal(actor, target, TeamAction.CHANGE_ROLE, sole_owner_id) is not None:
        return []
    return [role.value for role in actor.manageable_roles if role != target.role]


def invitable_roles(actor: Actor) -> list[str]:
    """The roles `actor` may invite someone as."""
    return [role.value for role in actor.manageable_roles]


def _locked(membership: Membership) -> Membership:
    """
    Re-read the row under a lock, so a rule is checked against what is there
    now and not against what the view loaded.

    Every owner row in the organization is locked first, in a fixed order.
    Two owners deactivating each other at the same moment would otherwise
    each see the other still standing, and leave the organization with nobody
    who can manage it. Taken for every team write, not only one aimed at an
    owner, so all of them queue in the same order and none can deadlock.
    """
    list(
        Membership.objects.select_for_update()
        .filter(organization_id=membership.organization_id, role=Role.OWNER)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return (
        Membership.objects.select_for_update(of=("self",))
        .select_related("user", "organization")
        .get(pk=membership.pk)
    )


def _require(actor: Actor, target: Membership, action: TeamAction) -> None:
    refusal = _refusal(actor, target, action, sole_active_owner_id(target.organization))
    if refusal is None:
        return
    if refusal.forbidden:
        raise PermissionDenied(refusal.detail)
    raise ConflictError(refusal.detail)


def _require_grantable(actor: Actor, role: str) -> None:
    not_manageable = _not_manageable(actor, role)
    if not_manageable:
        raise PermissionDenied(not_manageable)


# --------------------------------------------------------------------------
# The team page: the writes
# --------------------------------------------------------------------------


def invite(actor: Actor, organization, email: str, role: str) -> tuple[Membership, str]:
    """
    Invite `email` to `organization` as `role`. Returns the membership and
    the link that was emailed.

    The membership is created inactive and stays that way until the link is
    followed. That is what makes an invitation harmless to its recipient: the
    2FA policy, the magic-link rule and tenant resolution all read
    `is_active`, so until the person says yes nothing about how they sign in
    or what they reach has changed. Without it, any organization's admin
    could take a rival's customer off magic-link sign-in just by inviting
    them as staff.

    An address that already has an account gets a membership and nothing
    else: the user row is not written to, whatever organization it came from,
    and the response is the same as for a stranger.
    """
    _require_grantable(actor, role)
    email = email.strip().lower()

    try:
        with transaction.atomic():
            # `all_objects`: a soft-deleted user still owns the address, and
            # the unique index would refuse a second row for it.
            user = CustomUser.all_objects.filter(email=email).first()
            if user is None:
                user = CustomUser.objects.create_user(email=email)

            membership = Membership.all_objects.filter(user=user, organization=organization).first()
            if membership is not None and membership.deleted_at is None:
                raise ValidationError({"email": [ALREADY_ON_THE_TEAM]})
            if membership is None:
                membership = Membership(user=user, organization=organization)

            # Also the path back for a membership deleted in the admin: it
            # returns as an invitation, never as live access.
            membership.role = role
            membership.is_active = False
            membership.invited_at = timezone.now()
            membership.accepted_at = None
            membership.deactivated_at = None
            membership.deleted_at = None
            membership.save()

            _, raw_token = AccountToken.issue(AccountTokenPurpose.INVITE, user, membership)
    except IntegrityError as error:
        # Two people inviting the same address at the same moment.
        raise ConflictError("That person was invited a moment ago. Reload the team.") from error

    return membership, _deliver_invitation(membership, raw_token, invited_by=actor.user)


def change_role(actor: Actor, membership: Membership, role: str) -> Membership:
    with transaction.atomic():
        target = _locked(membership)
        _require(actor, target, TeamAction.CHANGE_ROLE)
        _require_grantable(actor, role)

        if role != target.role:
            target.role = role
            target.save(update_fields=["role", "updated_at"])
    return target


def deactivate(actor: Actor, membership: Membership) -> Membership:
    """
    Switch a membership off. For an invitation nobody accepted, withdraw it.

    The person's open sessions are not ended: they may belong to another
    organization. The middleware stops resolving this one on their very next
    request, which is what refuses them.
    """
    with transaction.atomic():
        target = _locked(membership)
        _require(actor, target, TeamAction.DEACTIVATE)

        target.is_active = False
        target.deactivated_at = timezone.now()
        target.save(update_fields=["is_active", "deactivated_at", "updated_at"])
        AccountToken.withdraw(AccountTokenPurpose.INVITE, user=target.user, membership=target)
    return target


def reactivate(actor: Actor, membership: Membership) -> tuple[Membership, str | None]:
    """
    Switch a membership back on. Returns it, and a link if one was emailed.

    Someone who had accepted is active again at once. A withdrawn invitation
    goes back to being an invitation, with a fresh link: reactivating must not
    be a way to hand someone access they never agreed to.
    """
    raw_token = None
    with transaction.atomic():
        target = _locked(membership)
        _require(actor, target, TeamAction.REACTIVATE)

        target.deactivated_at = None
        if target.awaiting_acceptance:
            target.invited_at = timezone.now()
            _, raw_token = AccountToken.issue(AccountTokenPurpose.INVITE, target.user, target)
        else:
            target.is_active = True
        target.save(update_fields=["is_active", "invited_at", "deactivated_at", "updated_at"])

    if raw_token is None:
        return target, None
    return target, _deliver_invitation(target, raw_token, invited_by=actor.user)


def resend_invite(actor: Actor, membership: Membership) -> tuple[Membership, str]:
    """Email a fresh link. The earlier one stops working (`AccountToken.issue`)."""
    with transaction.atomic():
        target = _locked(membership)
        _require(actor, target, TeamAction.RESEND_INVITE)

        target.invited_at = timezone.now()
        target.save(update_fields=["invited_at", "updated_at"])
        _, raw_token = AccountToken.issue(AccountTokenPurpose.INVITE, target.user, target)

    return target, _deliver_invitation(target, raw_token, invited_by=actor.user)


def _deliver_invitation(membership: Membership, raw_token: str, invited_by: CustomUser) -> str:
    """
    Email the invitation and return its link.

    Called after the transaction has committed, so a link is never in a
    mailbox for a row that was rolled back. A mail failure is logged without
    the link and not raised: the membership exists and Resend is one click.
    """
    link = f"{settings.FRONTEND_BASE_URL}/accept-invite/{raw_token}"
    site_name = getattr(settings, "SITE_NAME", "Pink Glove")
    organization = membership.organization

    body = render_to_string(
        "users/invite_email.html",
        {
            "organization_name": organization.name,
            "role": membership.get_role_display(),
            # Their name if they gave one; never their email address, which a
            # stranger receiving this has no business being sent.
            "invited_by": f"{invited_by.first_name} {invited_by.last_name}".strip(),
            "link": link,
            "site_name": site_name,
            "ttl_days": AccountToken.TTL[AccountTokenPurpose.INVITE].days,
        },
    )

    # Mirrors `two_factor.services.send_challenge`: locally the link comes
    # back in the response instead (`dev_link`), and nothing is sent.
    if not settings.LOCAL:
        try:
            message = EmailMessage(
                f"You have been invited to {organization.name} on {site_name}",
                body,
                settings.DEFAULT_FROM_EMAIL,
                [membership.user.email],
            )
            message.content_subtype = "html"
            message.send(fail_silently=False)
        except Exception:
            logger.exception("Failed to send invitation for membership %s", membership.pk)

    return link


# --------------------------------------------------------------------------
# Accepting an invitation
# --------------------------------------------------------------------------


def find_invitation(raw_token: str) -> AccountToken | None:
    """The live invitation behind a link, or None for every kind of no."""
    token = AccountToken.find(AccountTokenPurpose.INVITE, raw_token)
    if token is None:
        return None

    membership = token.membership
    if membership.deleted_at is not None or membership.state != MembershipState.INVITED:
        return None

    user = token.user
    if not user.is_active or user.deleted_at is not None:
        return None
    return token


def must_set_password(user: CustomUser) -> bool:
    """
    True when accepting is also this person's first time setting up the
    account: a new address, or a customer who has only ever used magic links.
    """
    return not user.has_usable_password()


def accept_invitation(
    raw_token: str, *, password: str = "", first_name: str = "", last_name: str = ""
) -> Membership:
    """
    Follow an invitation link: join the organization, and set up the account
    if it has never had a password.

    Nobody is signed in by this. The person signs in afterwards like anyone
    else and meets the ordinary challenge (ADR-008), so an intercepted
    invitation is not also a session.

    An account that already has a password keeps it, and its name: whatever
    was sent is ignored, because a link in a mailbox must not be a way to
    change either.
    """
    token = find_invitation(raw_token)
    if token is None:
        raise BadRequestJSON(INVALID_INVITATION)

    user = token.user
    claiming = must_set_password(user)

    if claiming:
        first_name, last_name = first_name.strip(), last_name.strip()
        errors = {}
        if not first_name:
            errors["first_name"] = ["Tell us your first name."]
        if not password:
            errors["password"] = ["Choose a password."]
        if errors:
            raise ValidationError(errors)

        # On the instance before validating, so the similarity validator
        # compares the password against the name being set as well.
        user.first_name, user.last_name = first_name, last_name
        validated_password(password, user)

    with transaction.atomic():
        # Locked so two invitations to one new address, accepted together,
        # cannot both believe they are the one setting the password.
        locked_user = CustomUser.objects.select_for_update().get(pk=user.pk)
        membership = (
            Membership.objects.select_for_update(of=("self",))
            .select_related("organization")
            .get(pk=token.membership_id)
        )
        if membership.state != MembershipState.INVITED or not token.consume():
            raise BadRequestJSON(INVALID_INVITATION)

        if claiming:
            if locked_user.has_usable_password():
                raise ConflictError(
                    "This account was set up a moment ago. Open the invitation again."
                )
            user.set_password(password)
            user.save(update_fields=["password", "first_name", "last_name", "updated_at"])

        membership.is_active = True
        membership.accepted_at = timezone.now()
        membership.save(update_fields=["is_active", "accepted_at", "updated_at"])

    return membership
