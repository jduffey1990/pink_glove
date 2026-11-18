"""
A deactivated membership is refused everywhere it could still count.

Three places can each keep a deactivated person working if they disagree: the
session payload (what the app offers), tenant resolution in the middleware
(what a request is scoped to), and a session that was already open when the
membership was switched off. Each is pinned here on its own.
"""

import pytest
from django.urls import reverse

from app.middleware.tenant import ORGANIZATION_HEADER
from users.enums import Role
from users.models import Membership

SESSION_URL = reverse("users:session")
DIRECTORY_URL = reverse("users:membership-list")


def _deactivate(user, organization):
    Membership.objects.filter(user=user, organization=organization).update(is_active=False)


@pytest.mark.django_db
class TestSessionPayload:
    def test_a_deactivated_membership_is_not_listed(self, api_client, owner, organization):
        _deactivate(owner, organization)
        api_client.force_login(owner)

        body = api_client.get(SESSION_URL).json()

        assert body["memberships"] == []
        assert body["current_organization"] is None
        assert body["current_role"] is None

    def test_the_other_membership_is_still_listed(
        self, api_client, owner, organization, other_organization
    ):
        """One organization switching someone off says nothing about another."""
        Membership.objects.create(user=owner, organization=other_organization, role=Role.CLEANER)
        _deactivate(owner, organization)
        api_client.force_login(owner)

        body = api_client.get(SESSION_URL).json()

        assert [row["organization"]["id"] for row in body["memberships"]] == [
            str(other_organization.id)
        ]
        # Exactly one active membership left, so it resolves without a header.
        assert body["current_organization"]["id"] == str(other_organization.id)
        assert body["current_role"] == Role.CLEANER


@pytest.mark.django_db
class TestTenantResolution:
    def test_naming_the_organization_in_the_header_does_not_bring_it_back(
        self, api_client, owner, organization
    ):
        _deactivate(owner, organization)
        api_client.force_login(owner)
        headers = {ORGANIZATION_HEADER.lower(): str(organization.id)}

        assert api_client.get(SESSION_URL, headers=headers).json()["current_organization"] is None
        assert api_client.get(DIRECTORY_URL, headers=headers).status_code == 403

    def test_a_deactivated_membership_does_not_make_the_other_one_ambiguous(
        self, api_client, owner, organization, other_organization
    ):
        Membership.objects.create(user=owner, organization=other_organization, role=Role.ADMIN)
        _deactivate(owner, organization)
        api_client.force_login(owner)

        rows = api_client.get(DIRECTORY_URL).json()["results"]

        assert {row["organization"]["id"] for row in rows} == {str(other_organization.id)}


@pytest.mark.django_db
class TestAnOpenSession:
    def test_it_is_refused_on_its_very_next_request(self, authed_client, owner, organization):
        """Nobody signs out and back in: the session stays, the access goes."""
        assert authed_client.get(DIRECTORY_URL).status_code == 200

        _deactivate(owner, organization)

        assert authed_client.get(DIRECTORY_URL).status_code == 403
        assert authed_client.get(reverse("customers:customer-list")).status_code == 403
        assert authed_client.get(reverse("scheduling:job-list")).status_code == 403

    def test_it_cannot_write_either(self, authed_client, owner, organization):
        _deactivate(owner, organization)

        response = authed_client.post(
            reverse("customers:customer-list"), {"first_name": "Nope"}, format="json"
        )

        assert response.status_code == 403
