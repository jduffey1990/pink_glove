import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone

from base.models import Base, SoftDeleteQuerySet, TenantModel
from users.enums import Role


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
    is_active = models.BooleanField(default=True)
    invited_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

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
