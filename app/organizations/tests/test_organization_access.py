"""
Who reads which organization payload (ADR-040).

Two ways the API describes the caller's organization: the settings endpoint
(`/api/organizations/current/`, the whole `Organization`) and the session
payload (`current_organization` and every membership's `organization`, the
narrow `OrganizationSummary`). The settings endpoint is the settings page's,
so it answers dispatcher and above and refuses cleaners and customers; the
session summary is everybody's, and carries everything a cleaner's or a
customer's screen reads, the read-only banner included.

The key sets are exact on purpose: a field added to either serializer has to
be added here too, which is the moment to decide who should see it.
"""

import pytest
from django.urls import reverse

from app.middleware.tenant import ORGANIZATION_HEADER
from users.enums import Role

URL = "/api/organizations/current/"
SESSION_URL = "/api/users/session/"

SETTINGS_KEYS = {
    "id",
    "name",
    "slug",
    "timezone",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "address_city",
    "address_state",
    "address_postal_code",
    "address_country",
    "logo",
    "primary_color",
    "business_hours_start",
    "business_hours_end",
    "working_days",
    "reveal_buffer_before_minutes",
    "reveal_buffer_after_minutes",
    "tax_rate_percent",
    "no_access_fee_type",
    "no_access_fee_value",
    "invoice_prefix",
    "invoice_terms_days",
    "invoice_footer",
    "visit_reminders_enabled",
    "stripe",
    "billing",
    "is_active",
    "created_at",
}
SUMMARY_KEYS = {"id", "name", "slug", "timezone", "primary_color", "logo", "is_active", "billing"}
NOTICE_KEYS = {"state", "level", "notice"}

READERS = (Role.OWNER, Role.ADMIN, Role.DISPATCHER)
REFUSED = (Role.CLEANER, Role.CUSTOMER)


@pytest.fixture
def as_role(api_client, organization, make_member):
    def _sign_in(role, org=None):
        api_client.force_login(make_member(org or organization, role))
        return api_client

    return _sign_in


@pytest.fixture
def deactivated(organization):
    organization.is_active = False
    organization.save()
    return organization


@pytest.mark.django_db
class TestTheSettingsEndpoint:
    @pytest.mark.parametrize("role", READERS)
    def test_the_settings_tiers_read_all_of_it(self, as_role, organization, role):
        response = as_role(role).get(URL)

        assert response.status_code == 200
        assert set(response.json()) == SETTINGS_KEYS
        assert response.json()["id"] == str(organization.id)

    @pytest.mark.parametrize("role", REFUSED)
    def test_cleaners_and_customers_read_none_of_it(self, as_role, role):
        response = as_role(role).get(URL)

        assert response.status_code == 403
        assert set(response.json()) == {"detail"}

    @pytest.mark.parametrize("role", REFUSED)
    def test_nor_by_another_method_that_reads(self, as_role, role):
        """HEAD and OPTIONS were always admin-tier; they stay refused below dispatcher."""
        client = as_role(role)

        assert client.head(URL).status_code == 403
        assert client.options(URL).status_code == 403

    @pytest.mark.parametrize("role", (Role.DISPATCHER, *REFUSED))
    def test_writes_stay_admin_and_owner(self, as_role, organization, role):
        response = as_role(role).patch(URL, {"name": "Renamed"}, format="json")

        assert response.status_code == 403
        organization.refresh_from_db()
        assert organization.name == "Sparkle Clean"


@pytest.mark.django_db
class TestTheSessionSummary:
    @pytest.mark.parametrize("role", (*READERS, *REFUSED))
    def test_every_role_gets_the_summary_and_only_the_summary(self, as_role, organization, role):
        body = as_role(role).get(SESSION_URL).json()

        current = body["current_organization"]
        assert set(current) == SUMMARY_KEYS
        assert set(current["billing"]) == NOTICE_KEYS
        assert current["id"] == str(organization.id)
        assert [set(m["organization"]) for m in body["memberships"]] == [SUMMARY_KEYS]
        assert body["current_role"] == role

    @pytest.mark.parametrize("role", (*READERS, *REFUSED))
    def test_a_deactivated_organization_still_gives_every_role_its_banner(
        self, as_role, deactivated, role
    ):
        """The shell draws read-only and the notice from the session, so no role needs the endpoint."""
        current = as_role(role).get(SESSION_URL).json()["current_organization"]

        assert current["is_active"] is False
        assert set(current["billing"]) == NOTICE_KEYS
        assert current["billing"]["state"] == "deactivated"
        assert current["timezone"] == deactivated.timezone


@pytest.mark.django_db
class TestADeactivatedOrganization:
    @pytest.mark.parametrize("role", READERS)
    def test_the_settings_tiers_can_still_read_it(self, as_role, deactivated, role):
        response = as_role(role).get(URL)

        assert response.status_code == 200
        assert set(response.json()) == SETTINGS_KEYS
        assert response.json()["is_active"] is False

    @pytest.mark.parametrize("role", REFUSED)
    def test_deactivation_opens_nothing_below_them(self, as_role, deactivated, role):
        assert as_role(role).get(URL).status_code == 403


@pytest.mark.django_db
class TestTenancy:
    @pytest.mark.parametrize("role", (*READERS, *REFUSED))
    def test_a_member_never_receives_another_organizations(
        self, as_role, organization, other_organization, role
    ):
        client = as_role(role)
        foreign = {ORGANIZATION_HEADER: str(other_organization.id)}

        settings = client.get(URL, headers=foreign)
        session = client.get(SESSION_URL, headers=foreign)
        own = client.get(SESSION_URL)

        assert settings.status_code == 403
        for response in (settings, session, own):
            assert str(other_organization.id) not in response.content.decode()
            assert other_organization.name not in response.content.decode()

    def test_each_reader_gets_their_own(
        self, api_client, make_member, organization, other_organization
    ):
        for org in (organization, other_organization):
            api_client.force_login(make_member(org, Role.DISPATCHER))

            assert api_client.get(URL).json()["id"] == str(org.id)
            api_client.logout()


def test_the_endpoint_is_the_one_the_tests_name():
    assert reverse("organizations:current") == URL
