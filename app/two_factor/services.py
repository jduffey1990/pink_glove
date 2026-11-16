"""
Policy and delivery for two-factor challenges, and the staff password reset.

The reset lives here, beside the sign-in it recovers, because finishing one
has to forget this app's trusted devices and pending codes. `users` knows
nothing about either, and `two_factor` already depends on `users`, so the
dependency keeps running one way (ADR-028).
"""

import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from app.exceptions import BadRequestJSON
from two_factor.models import TrustedDevice, TwoFactorCode
from users.enums import ALWAYS_TWO_FACTOR_ROLES, AccountTokenPurpose, Role
from users.models import AccountToken, CustomUser, Membership
from users.services import may_reset_password, validated_password

logger = logging.getLogger(__name__)

#: One sentence for every way a reset link can be no good, as with an
#: invitation: unknown, expired, used, replaced by a newer one, or issued
#: for an account that may no longer reset.
INVALID_RESET_LINK = "That reset link is invalid or has expired. Ask for a new one."

#: Name of the cookie holding a trusted-device token.
TRUSTED_DEVICE_COOKIE = "pg_device"


def two_factor_required(user: CustomUser, device_token: str | None) -> bool:
    """
    Decide whether this sign-in needs a challenge.

    One policy does not fit three audiences (docs/DECISIONS.md ADR-008):

    - office staff (owner / admin / dispatcher) are challenged every time;
      they handle customer and financial data
    - cleaners are challenged only on an untrusted device; a challenge at
      every job site is friction that gets the app abandoned
    - customers never reach here at all -- they use magic links
    """
    roles = set(Membership.objects.filter(user=user, is_active=True).values_list("role", flat=True))

    if user.is_superuser or roles & set(ALWAYS_TWO_FACTOR_ROLES):
        return True

    if roles == {Role.CUSTOMER}:
        return False

    # Cleaners, and users with no membership yet.
    return not TrustedDevice.is_trusted(user, device_token)


def send_challenge(user: CustomUser, resent: bool = False) -> tuple[TwoFactorCode, str]:
    """Issue a challenge and email it. Returns the challenge and raw code."""
    challenge, raw_code = TwoFactorCode.issue(user)

    site_name = getattr(settings, "SITE_NAME", "Pink Glove")
    # The code is in the body only. A subject is what a lock screen previews
    # and what mail servers log, and neither should be holding a live code.
    subject = f"Your {site_name} sign-in code"
    if resent:
        subject += " (resent)"

    body = render_to_string(
        "two_factor/code_email.html",
        {
            "first_name": user.first_name,
            "code": raw_code,
            "site_name": site_name,
            "ttl_minutes": int(TwoFactorCode.TTL.total_seconds() // 60),
        },
    )

    if not settings.LOCAL:
        try:
            message = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email])
            message.content_subtype = "html"
            message.send(fail_silently=False)
        except Exception:
            # Logged, not raised: the challenge exists and the user can ask for
            # a resend. Failing the request would leak that the account exists.
            logger.exception("Failed to send 2FA email to user %s", user.pk)

    return challenge, raw_code


# --------------------------------------------------------------------------
# Staff password reset
# --------------------------------------------------------------------------


def issue_password_reset(email: str) -> tuple[CustomUser, str] | None:
    """
    Issue a reset link for `email`. None when that address does not get one:
    no such account, a customer, a platform superuser, or nobody's staff.

    Issuing withdraws any earlier link (`AccountToken.issue`), so asking
    twice leaves one working email, not two.
    """
    user = CustomUser.objects.filter(email=email.strip().lower()).first()
    if user is None or not may_reset_password(user):
        return None

    _, raw_token = AccountToken.issue(AccountTokenPurpose.PASSWORD_RESET, user)
    return user, f"{settings.FRONTEND_BASE_URL}/reset-password/{raw_token}"


def send_password_reset(user: CustomUser, link: str) -> None:
    """Email the link. A failure is logged without it and never raised."""
    site_name = getattr(settings, "SITE_NAME", "Pink Glove")
    ttl = AccountToken.TTL[AccountTokenPurpose.PASSWORD_RESET]

    body = render_to_string(
        "two_factor/password_reset_email.html",
        {
            "first_name": user.first_name,
            "link": link,
            "site_name": site_name,
            "ttl_minutes": int(ttl.total_seconds() // 60),
        },
    )

    try:
        message = EmailMessage(
            f"Reset your {site_name} password", body, settings.DEFAULT_FROM_EMAIL, [user.email]
        )
        message.content_subtype = "html"
        message.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send password reset to user %s", user.pk)


def forget_devices(user: CustomUser) -> None:
    """
    Withdraw everything that would let a sign-in skip or finish a challenge.

    Trusted devices, so every device is challenged again, and codes already
    issued, so a sign-in started with the old password cannot be completed.
    """
    now = timezone.now()
    TrustedDevice.objects.filter(user=user).delete()
    TwoFactorCode.objects.filter(user=user, consumed_at__isnull=True).update(
        consumed_at=now, updated_at=now
    )


def reset_password(raw_token: str, password: str) -> None:
    """
    Finish a reset: set the password, and make everything before it worthless.

    Nobody is signed in. The person signs in afterwards with the new password
    and meets the ordinary challenge, on every device, so a reset is never a
    way around the second factor.

    Every session the account had open ends with the password: Django ties a
    session to a hash of it (`get_session_auth_hash`), and nothing here
    refreshes that hash for anyone.
    """
    token = AccountToken.find(AccountTokenPurpose.PASSWORD_RESET, raw_token)
    # Asked again here, as the magic link does: a person can be deactivated,
    # or become a customer only, while a link is live.
    if token is None or not may_reset_password(token.user):
        raise BadRequestJSON(INVALID_RESET_LINK)

    user = token.user
    # Before the token is spent, so a password the validators refuse does not
    # cost the person their link.
    validated_password(password, user)

    with transaction.atomic():
        if not token.consume():
            raise BadRequestJSON(INVALID_RESET_LINK)
        user.set_password(password)
        user.save(update_fields=["password", "updated_at"])
        forget_devices(user)
