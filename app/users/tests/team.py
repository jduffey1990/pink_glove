"""Shared helpers for the team-management tests. Not a test module."""

from urllib.parse import parse_qs, urlsplit

from django.urls import reverse
from rest_framework.test import APIClient

from users.enums import Role
from users.models import CustomUser, Membership

LIST = reverse("users:membership-list")
INVITE_OPTIONS = reverse("users:membership-invite-options")
INSPECT = reverse("users:invitation-inspect")
ACCEPT = reverse("users:invitation-accept")
SESSION = reverse("users:session")

STAFF = (Role.OWNER, Role.ADMIN, Role.DISPATCHER, Role.CLEANER)
EVERY_ROLE = (*STAFF, Role.CUSTOMER)

#: Long, and unlike any name or address used here, so it clears every
#: validator in AUTH_PASSWORD_VALIDATORS. A phrase with spaces, so the secret
#: scanner in pre-commit does not take a test fixture for a credential.
GOOD_PASSWORD = "a long phrase for tests only"  # noqa: S105


def detail(membership) -> str:
    return reverse("users:membership-detail", args=[membership.id])


def action(membership, name: str) -> str:
    return reverse(f"users:membership-{name}", args=[membership.id])


def member(organization, role, email=None, **fields) -> Membership:
    """A user with a membership, returning the membership."""
    user = CustomUser.objects.create_user(
        email=email or f"{role}-{organization.slug}@example.com",
        password="pw-for-tests-only",  # noqa: S106
        first_name=str(role).title(),
        last_name="Person",
    )
    return Membership.objects.create(user=user, organization=organization, role=role, **fields)


def client_for(membership_or_user) -> APIClient:
    """A client with a real session (never `force_authenticate`)."""
    client = APIClient()
    client.force_login(getattr(membership_or_user, "user", membership_or_user))
    return client


def token_of(link: str) -> str:
    """The token in an emailed link's fragment (`...#token=<raw>`, ADR-036)."""
    fragment = urlsplit(link).fragment
    return parse_qs(fragment)["token"][0]


def invite(client, email="new.person@example.com", role=Role.CLEANER, headers=None, **extra):
    """`extra` goes in the body, which is how the mass-assignment tests forge fields."""
    return client.post(
        LIST, {"email": email, "role": role, **extra}, format="json", headers=headers
    )
