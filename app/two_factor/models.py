"""
Two-factor authentication.

Ported from the source repo's `two_factor` app and hardened. That version
stored codes in plaintext, allowed unlimited guesses, had no rate limiting, and
returned the code in the API response in every non-production environment.

Note these extend `Base`, not `TenantModel`: 2FA happens before the tenant is
resolved, so there is no organization to scope to yet.
"""

import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone

from base.models import Base
from users.models import CustomUser


def _hash_device_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


class TwoFactorCode(Base):
    """A single 6-digit challenge."""

    TTL = timedelta(minutes=10)
    MAX_ATTEMPTS = 5
    CODE_LENGTH = 6

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="two_factor_codes")
    #: A 6-digit code carries ~20 bits of entropy, which IS brute-forceable --
    #: so this uses Django's configured (slow) password hasher, not the SHA-256
    #: used for high-entropy tokens elsewhere.
    code_hash = models.CharField(max_length=255)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta(Base.Meta):
        verbose_name = "Two-factor code"
        verbose_name_plural = "Two-factor codes"
        ordering = ("-created_at",)

    def __str__(self):
        return f"2FA challenge for {self.user.email}"

    @classmethod
    def issue(cls, user: CustomUser) -> tuple["TwoFactorCode", str]:
        """Create a challenge, returning it with the raw code to send."""
        raw_code = f"{secrets.randbelow(10**cls.CODE_LENGTH):0{cls.CODE_LENGTH}d}"
        challenge = cls.objects.create(
            user=user,
            code_hash=make_password(raw_code),
            expires_at=timezone.now() + cls.TTL,
        )
        return challenge, raw_code

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_usable(self) -> bool:
        return (
            self.consumed_at is None and not self.is_expired and self.attempts < self.MAX_ATTEMPTS
        )

    def verify(self, submitted_code: str) -> bool:
        """
        Check a submitted code, recording the attempt either way.

        Always increments `attempts` first, so a client that abandons the
        request mid-flight still burns its guess.
        """
        if not self.is_usable:
            return False

        # Counted in the database, not on this instance: parallel requests each
        # load `attempts` before any of them saves, and a read-modify-write
        # would let all of them through on the same count.
        now = timezone.now()
        live = type(self).objects.filter(
            pk=self.pk, consumed_at__isnull=True, attempts__lt=self.MAX_ATTEMPTS
        )
        if not live.update(attempts=models.F("attempts") + 1, updated_at=now):
            return False
        self.refresh_from_db(fields=["attempts"])

        if not check_password(submitted_code, self.code_hash):
            return False

        # Conditional for the same reason: only one request gets to consume it.
        if (
            not type(self)
            .objects.filter(pk=self.pk, consumed_at__isnull=True)
            .update(consumed_at=now, updated_at=now)
        ):
            return False
        self.consumed_at = now
        return True


class TrustedDevice(Base):
    """
    A device that has already cleared a 2FA challenge.

    This is what makes 2FA workable for cleaners: challenge on a new device,
    not every time they open the app in a driveway. Office staff are challenged
    regardless -- see `users.enums.ALWAYS_TWO_FACTOR_ROLES`.
    """

    TTL = timedelta(days=30)

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="trusted_devices")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta(Base.Meta):
        verbose_name = "Trusted device"
        verbose_name_plural = "Trusted devices"
        ordering = ("-created_at",)

    def __str__(self):
        return f"Trusted device for {self.user.email}"

    @classmethod
    def issue(cls, user: CustomUser, user_agent: str = "") -> tuple["TrustedDevice", str]:
        raw_token = secrets.token_urlsafe(32)
        device = cls.objects.create(
            user=user,
            token_hash=_hash_device_token(raw_token),
            expires_at=timezone.now() + cls.TTL,
            user_agent=user_agent[:512],
        )
        return device, raw_token

    @classmethod
    def is_trusted(cls, user: CustomUser, raw_token: str | None) -> bool:
        """True if the token names a live trusted device for this user."""
        if not raw_token:
            return False

        device = cls.objects.filter(
            user=user,
            token_hash=_hash_device_token(raw_token),
            expires_at__gt=timezone.now(),
        ).first()

        if device is None:
            return False

        device.last_used_at = timezone.now()
        device.save(update_fields=["last_used_at", "updated_at"])
        return True
