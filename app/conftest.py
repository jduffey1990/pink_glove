"""Shared pytest fixtures."""

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def organization(db):
    from organizations.models import Organization

    return Organization.objects.create(name="Sparkle Clean", timezone="America/Denver")


@pytest.fixture
def other_organization(db):
    from organizations.models import Organization

    return Organization.objects.create(name="Rival Cleaners", timezone="America/New_York")


@pytest.fixture
def user(db):
    from users.models import CustomUser

    return CustomUser.objects.create_user(
        email="owner@example.com",
        password="pw-for-tests-only",
        first_name="Pat",
        last_name="Rivera",
    )


@pytest.fixture
def superuser(db):
    from users.models import CustomUser

    return CustomUser.objects.create_superuser(
        email="admin@example.com",
        password="pw-for-tests-only",
    )


@pytest.fixture
def make_member(db):
    """Build a user with a membership in a given organization."""
    from users.enums import Role
    from users.models import CustomUser, Membership

    def _make(organization, role=Role.OWNER, email=None, password="pw-for-tests-only"):
        user = CustomUser.objects.create_user(
            email=email or f"{role}-{organization.slug}@example.com",
            password=password,
        )
        Membership.objects.create(user=user, organization=organization, role=role)
        return user

    return _make


@pytest.fixture
def owner(organization, make_member):
    return make_member(organization)


@pytest.fixture
def rival_owner(other_organization, make_member):
    return make_member(other_organization)


@pytest.fixture
def authed_client(api_client, owner):
    """
    Signed in via a real session.

    NOT `force_authenticate`: that attaches the user during DRF view dispatch,
    which happens *after* Django middleware, so TenantMiddleware would still
    see AnonymousUser and resolve no organization.
    """
    api_client.force_login(owner)
    return api_client
