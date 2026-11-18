"""
The visit-reminder switch (ADR-035): off by default, on the existing
organization endpoint, and changed by owners and admins only.

What a reminder is and who gets one is `notifications/tests/`; this is the
switch and its gating.
"""

import pytest

from app.middleware.tenant import ORGANIZATION_HEADER
from base.permissions import ORGANIZATION_INACTIVE
from organizations.models import Organization
from users.enums import Role

URL = "/api/organizations/current/"


def turn_on(api_client):
    return api_client.patch(URL, {"visit_reminders_enabled": True}, format="json")


@pytest.mark.django_db
class TestDefault:
    def test_a_new_organization_sends_no_reminders(self):
        """No tenant finds its customers emailed by something it never switched on."""
        assert Organization.objects.create(name="Fresh Clean").visit_reminders_enabled is False

    @pytest.mark.parametrize("role", list(Role))
    def test_every_member_can_read_it(self, api_client, organization, make_member, role):
        api_client.force_login(make_member(organization, role))

        response = api_client.get(URL)

        assert response.status_code == 200
        assert response.data["visit_reminders_enabled"] is False


@pytest.mark.django_db
class TestWhoMayChangeIt:
    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
    def test_an_owner_or_admin_can_turn_it_on_and_off(
        self, api_client, organization, make_member, role
    ):
        api_client.force_login(make_member(organization, role))

        response = turn_on(api_client)

        assert response.status_code == 200
        assert response.data["visit_reminders_enabled"] is True
        organization.refresh_from_db()
        assert organization.visit_reminders_enabled is True

        response = api_client.patch(URL, {"visit_reminders_enabled": False}, format="json")

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.visit_reminders_enabled is False

    @pytest.mark.parametrize("role", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_nobody_below_admin_can(self, api_client, organization, make_member, role):
        api_client.force_login(make_member(organization, role))

        response = turn_on(api_client)

        assert response.status_code == 403
        organization.refresh_from_db()
        assert organization.visit_reminders_enabled is False

    def test_an_anonymous_caller_cannot(self, api_client, organization):
        response = turn_on(api_client)

        assert response.status_code in (401, 403)
        organization.refresh_from_db()
        assert organization.visit_reminders_enabled is False


@pytest.mark.django_db
class TestTenancy:
    def test_it_changes_only_the_callers_organization(
        self, api_client, organization, other_organization, make_member
    ):
        api_client.force_login(make_member(organization, Role.OWNER))

        assert turn_on(api_client).status_code == 200

        other_organization.refresh_from_db()
        assert other_organization.visit_reminders_enabled is False

    def test_naming_another_organization_reaches_nothing_of_it(
        self, api_client, organization, other_organization, make_member
    ):
        """The header is checked against the caller's own memberships."""
        api_client.force_login(make_member(organization, Role.OWNER))

        response = api_client.patch(
            URL,
            {"visit_reminders_enabled": True},
            format="json",
            headers={ORGANIZATION_HEADER: str(other_organization.id)},
        )

        assert response.status_code in (403, 404)
        other_organization.refresh_from_db()
        assert other_organization.visit_reminders_enabled is False


@pytest.mark.django_db
class TestReadOnlyOrganization:
    """A deactivated organization is read-only (ADR-028), and this is no exception."""

    @pytest.fixture
    def deactivated(self, organization):
        organization.is_active = False
        organization.save(update_fields=["is_active", "updated_at"])
        return organization

    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
    def test_the_switch_cannot_be_moved(self, api_client, deactivated, make_member, role):
        api_client.force_login(make_member(deactivated, role))

        response = turn_on(api_client)

        assert response.status_code == 403
        assert response.data["detail"] == ORGANIZATION_INACTIVE
        deactivated.refresh_from_db()
        assert deactivated.visit_reminders_enabled is False

    def test_it_can_still_be_read(self, api_client, deactivated, make_member):
        api_client.force_login(make_member(deactivated, Role.OWNER))

        response = api_client.get(URL)

        assert response.status_code == 200
        assert response.data["visit_reminders_enabled"] is False
