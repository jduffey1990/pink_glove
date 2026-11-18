import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models, transaction
from django.utils import timezone

from base.models import Base, SoftDeleteQuerySet, TenantModel
from users.enums import AccountTokenPurpose, MembershipState, Role


class CustomUserManager(BaseUserManager.from_queryset(SoftDeleteQuerySet)):
    """Email-keyed user manager. Soft-deleted users are excluded by default."""

    use_in_migrations = True

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        extra_fields.setdefault("is_active", True)

        user = self.model(email=self.normalize_email(email).lower(), **extra_fields)
        if password:
            user.set_password(password)
        else:
            # Customers authenticate by magic link and never set a password.
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        if not password:
            raise ValueError("Superuser must have a password.")

        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin, Base):
    """
    An authenticating identity. Deliberately holds no organization and no role
    -- those live on `Membership` (Phase 1) so one person can belong to several
    organizations. See docs/DECISIONS.md ADR-003.
    """

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False,
        help_text="Can sign in to the Django admin. Unrelated to organization role.",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    class Meta(Base.Meta):
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self):
        return self.email

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.first_name or self.email

    def save(self, *args, **kwargs):
        self.email = self.email.lower()
        super().save(*args, **kwargs)


class Membership(TenantModel):
    """
    A user's role within one organization.

    Role lives here rather than on `CustomUser` so the same person can hold
    different roles in different organizations. See docs/DECISIONS.md ADR-003.
    """

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=32, choices=Role.choices, db_index=True)
    #: What every access check reads (the tenant middleware, the 2FA policy,
    #: the magic-link rule, the assignee checks). An invitation is created
    #: with this False and only acceptance sets it, so an invitation nobody
    #: has accepted changes nothing about how its recipient signs in or what
    #: they can reach, in this organization or any other (ADR-034).
    is_active = models.BooleanField(default=True)
    #: Both are NULL on a membership made by the seed or in the admin, which
    #: is an ordinary active membership. Nothing gates sign-in on either.
    invited_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    #: Set when someone on the team switches this membership off, cleared when
    #: it is switched back on. It is what tells a withdrawn invitation from a
    #: pending one, since `is_active` is False for both.
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        verbose_name = "Membership"
        verbose_name_plural = "Memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"], name="unique_membership_per_organization"
            )
        ]

    def __str__(self):
        return f"{self.user.email} @ {self.organization_id} ({self.role})"

    @property
    def is_staff_role(self) -> bool:
        return self.role != Role.CUSTOMER

    @property
    def awaiting_acceptance(self) -> bool:
        """Invited, and the invitation has never been accepted."""
        return self.invited_at is not None and self.accepted_at is None

    @property
    def state(self) -> MembershipState:
        if self.is_active:
            return MembershipState.ACTIVE
        if self.awaiting_acceptance and self.deactivated_at is None:
            return MembershipState.INVITED
        # Includes a membership switched off in the admin, which has no
        # `deactivated_at`: inactive and not a pending invitation.
        return MembershipState.DEACTIVATED


def _hash_token(raw_token: str) -> str:
    """
    SHA-256 is correct here, unlike for passwords.

    These tokens carry 256 bits of entropy, so there is nothing to brute-force
    and a deliberately slow hash would only cost latency. Contrast
    `two_factor.models.TwoFactorCode`, where a 6-digit code has ~20 bits and
    therefore does need a slow hasher.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


class MagicLinkToken(Base):
    """
    Single-use passwordless sign-in, used for the customer portal.

    Customers check an invoice a few times a year; a password they must reset
    every time is pure support burden. See docs/DECISIONS.md ADR-008.

    Only the hash is stored -- a database leak must not yield usable links.
    """

    TTL = timedelta(minutes=15)

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="magic_links")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta(Base.Meta):
        verbose_name = "Magic link token"
        verbose_name_plural = "Magic link tokens"

    def __str__(self):
        return f"Magic link for {self.user.email}"

    @classmethod
    def issue(cls, user: "CustomUser") -> tuple["MagicLinkToken", str]:
        """Create a token and return it alongside the raw value to email."""
        raw_token = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            token_hash=_hash_token(raw_token),
            expires_at=timezone.now() + cls.TTL,
        )
        return token, raw_token

    @classmethod
    def consume(cls, raw_token: str) -> "CustomUser | None":
        """Redeem a token, returning its user. Returns None if unusable."""
        token = cls.objects.filter(token_hash=_hash_token(raw_token)).first()
        if token is None or not token.is_valid:
            return None

        # A conditional update, so two requests racing on one link cannot both
        # pass the `is_valid` check above and both be signed in.
        now = timezone.now()
        if not cls.objects.filter(pk=token.pk, consumed_at__isnull=True).update(
            consumed_at=now, updated_at=now
        ):
            return None
        return token.user

    @property
    def is_valid(self) -> bool:
        return self.consumed_at is None and timezone.now() <= self.expires_at


class AccountToken(Base):
    """
    A single-use emailed link that sets a staff password: an invitation to an
    organization, or a password reset (ADR-034).

    A sibling of `MagicLinkToken` rather than a generalisation of it, built
    the same way: 256 random bits, only the SHA-256 stored, an expiry, and a
    conditional update so two requests racing on one link cannot both win.
    Kept apart for two reasons. A magic link signs its holder in and these
    never do, so sharing a table would leave "which kind is this" to a filter
    somebody can forget, and the failure is a staff session from a link meant
    for something else. And the customer's sign-in path is not rewritten to
    make room for a feature it has nothing to do with.

    `purpose` is a required argument of every lookup, so a token is only ever
    found by the endpoint it was issued for: an invitation presented as a
    reset is simply not there. An invitation also names the one membership it
    opens; a reset names none.

    `Base`, not `TenantModel`, like the other sign-in rows: a reset happens
    before any tenant is resolved, and no API serves these.
    """

    TTL = {
        AccountTokenPurpose.INVITE: timedelta(days=7),
        AccountTokenPurpose.PASSWORD_RESET: timedelta(hours=1),
    }

    purpose = models.CharField(max_length=32, choices=AccountTokenPurpose.choices)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="account_tokens")
    membership = models.ForeignKey(
        Membership,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="invite_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    #: Stamped when the link is used, and also when it is withdrawn: by a
    #: resend, by a newer reset request, or by the membership being switched
    #: off. Either way the link is spent.
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta(Base.Meta):
        verbose_name = "Account token"
        verbose_name_plural = "Account tokens"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(purpose=AccountTokenPurpose.INVITE, membership__isnull=False)
                    | (
                        ~models.Q(purpose=AccountTokenPurpose.INVITE)
                        & models.Q(membership__isnull=True)
                    )
                ),
                name="account_token_only_an_invite_names_a_membership",
            )
        ]

    def __str__(self):
        return f"{self.get_purpose_display()} for {self.user.email}"

    @classmethod
    def issue(
        cls,
        purpose: AccountTokenPurpose,
        user: "CustomUser",
        membership: "Membership | None" = None,
    ) -> tuple["AccountToken", str]:
        """
        Create a token and return it alongside the raw value to email.

        Withdraws whatever was live for the same thing first, so there is
        only ever one working link per invitation and one per reset: a resend
        kills the earlier email, and so does asking for a reset twice.
        """
        raw_token = secrets.token_urlsafe(32)
        with transaction.atomic():
            cls.withdraw(purpose, user=user, membership=membership)
            token = cls.objects.create(
                purpose=purpose,
                user=user,
                membership=membership,
                token_hash=_hash_token(raw_token),
                expires_at=timezone.now() + cls.TTL[purpose],
            )
        return token, raw_token

    @classmethod
    def withdraw(
        cls,
        purpose: AccountTokenPurpose,
        *,
        user: "CustomUser",
        membership: "Membership | None" = None,
    ) -> int:
        """Spend every live token for this invitation, or this user's resets."""
        live = cls.objects.filter(purpose=purpose, user=user, consumed_at__isnull=True)
        if membership is not None:
            live = live.filter(membership=membership)
        now = timezone.now()
        return live.update(consumed_at=now, updated_at=now)

    @classmethod
    def find(cls, purpose: AccountTokenPurpose, raw_token: str) -> "AccountToken | None":
        """
        The live token for this purpose, or None. Spends nothing.

        Separate from `consume` because both flows validate a password between
        the two: a password the validators refuse must not cost the person
        their link.
        """
        token = (
            cls.objects.filter(purpose=purpose, token_hash=_hash_token(raw_token))
            .select_related("user", "membership__organization")
            .first()
        )
        if token is None or not token.is_valid:
            return None
        return token

    def consume(self) -> bool:
        """Spend this token. False if another request got there first."""
        # Conditional for the same reason as `MagicLinkToken.consume`.
        now = timezone.now()
        spent = (
            type(self)
            .objects.filter(pk=self.pk, consumed_at__isnull=True, expires_at__gte=now)
            .update(consumed_at=now, updated_at=now)
        )
        if spent:
            self.consumed_at = now
        return bool(spent)

    @property
    def is_valid(self) -> bool:
        return self.consumed_at is None and timezone.now() <= self.expires_at
