"""Sign-in rules that more than one view has to agree on."""

import logging

from django.conf import settings
from django.core.mail import send_mail

from users.enums import Role
from users.models import CustomUser, MagicLinkToken, Membership

logger = logging.getLogger(__name__)


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
