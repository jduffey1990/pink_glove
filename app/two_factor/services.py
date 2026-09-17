"""Policy and delivery for two-factor challenges."""

import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

from two_factor.models import TrustedDevice, TwoFactorCode
from users.enums import ALWAYS_TWO_FACTOR_ROLES, Role
from users.models import CustomUser, Membership

logger = logging.getLogger(__name__)

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
