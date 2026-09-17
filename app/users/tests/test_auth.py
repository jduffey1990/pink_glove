import pytest
from django.urls import reverse

from app.middleware.tenant import ORGANIZATION_HEADER
from users.enums import Role
from users.models import MagicLinkToken, Membership

SESSION_URL = reverse("users:session")
MAGIC_LINK_URL = reverse("users:magic-link-request")
MAGIC_CONSUME_URL = reverse("users:magic-link-consume")


@pytest.mark.django_db
class TestSessionView:
    def test_anonymous_gets_an_empty_body(self, api_client):
        response = api_client.get(SESSION_URL)

        assert response.status_code == 200
        assert response.json() == {}

    def test_signed_in_user_sees_their_organization_and_role(self, authed_client, organization):
        body = authed_client.get(SESSION_URL).json()

        assert body["current_organization"]["name"] == organization.name
        assert body["current_role"] == Role.OWNER
        assert len(body["memberships"]) == 1

    def test_logout_ends_the_session(self, authed_client):
        assert authed_client.post(reverse("users:logout")).status_code == 200
        assert authed_client.get(SESSION_URL).json() == {}


@pytest.mark.django_db
class TestTenantResolution:
    def test_single_membership_resolves_without_a_header(self, authed_client, organization):
        body = authed_client.get(SESSION_URL).json()

        assert body["current_organization"]["id"] == str(organization.id)

    def test_user_with_no_membership_has_no_organization(self, api_client, user):
        api_client.force_login(user)

        assert api_client.get(SESSION_URL).json()["current_organization"] is None

    def test_multiple_memberships_are_ambiguous_without_a_header(
        self, api_client, owner, other_organization
    ):
        from users.models import Membership

        Membership.objects.create(user=owner, organization=other_organization, role=Role.ADMIN)
        api_client.force_login(owner)

        # Ambiguous on purpose: guessing would be worse than asking.
        assert api_client.get(SESSION_URL).json()["current_organization"] is None

    def test_header_disambiguates(self, api_client, owner, other_organization):
        from users.models import Membership

        Membership.objects.create(user=owner, organization=other_organization, role=Role.ADMIN)
        api_client.force_login(owner)

        body = api_client.get(
            SESSION_URL, headers={ORGANIZATION_HEADER.lower(): str(other_organization.id)}
        ).json()

        assert body["current_organization"]["id"] == str(other_organization.id)
        assert body["current_role"] == Role.ADMIN

    def test_header_for_an_organization_you_do_not_belong_to_is_ignored(
        self, api_client, owner, other_organization
    ):
        api_client.force_login(owner)

        body = api_client.get(
            SESSION_URL, headers={ORGANIZATION_HEADER.lower(): str(other_organization.id)}
        ).json()

        assert body["current_organization"] is None

    def test_a_malformed_header_does_not_error(self, authed_client):
        response = authed_client.get(
            SESSION_URL, headers={ORGANIZATION_HEADER.lower(): "not-a-uuid"}
        )

        assert response.status_code == 200

    def test_superuser_may_act_on_any_organization(self, api_client, superuser, other_organization):
        api_client.force_login(superuser)

        body = api_client.get(
            SESSION_URL, headers={ORGANIZATION_HEADER.lower(): str(other_organization.id)}
        ).json()

        assert body["current_organization"]["id"] == str(other_organization.id)
        assert body["current_role"] is None


@pytest.mark.django_db
class TestMagicLink:
    @pytest.fixture(autouse=True)
    def _local_mode(self, settings):
        """LOCAL exposes dev_link, which is how these tests learn the token."""
        settings.LOCAL = True

    @pytest.fixture
    def homeowner(self, organization, make_member):
        return make_member(organization, role=Role.CUSTOMER, email="homeowner@example.com")

    def _request_link(self, api_client, email):
        return api_client.post(MAGIC_LINK_URL, {"email": email})

    def _token_for(self, api_client, user):
        return self._request_link(api_client, user.email).json()["dev_link"].rsplit("/", 1)[-1]

    def test_unknown_address_looks_identical_to_a_known_one(self, api_client, homeowner):
        known = self._request_link(api_client, homeowner.email)
        unknown = self._request_link(api_client, "nobody@example.com")

        assert known.status_code == unknown.status_code == 202
        assert known.json()["detail"] == unknown.json()["detail"]
        assert MagicLinkToken.objects.count() == 1

    def test_a_valid_link_signs_the_user_in(self, api_client, homeowner):
        response = api_client.post(
            MAGIC_CONSUME_URL, {"token": self._token_for(api_client, homeowner)}
        )

        assert response.status_code == 200
        assert response.json()["email"] == homeowner.email

    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.DISPATCHER, Role.CLEANER])
    def test_staff_are_never_issued_a_link(self, api_client, organization, make_member, role):
        """ADR-008: a link is one factor, and would stand in for password + 2FA."""
        staff = make_member(organization, role=role)

        response = self._request_link(api_client, staff.email)

        assert response.status_code == 202
        assert "dev_link" not in response.json()
        assert not MagicLinkToken.objects.exists()

    def test_a_superuser_is_never_issued_a_link(self, api_client, superuser):
        self._request_link(api_client, superuser.email)

        assert not MagicLinkToken.objects.exists()

    def test_a_customer_who_is_also_staff_elsewhere_is_not_issued_a_link(
        self, api_client, homeowner, other_organization
    ):
        Membership.objects.create(
            user=homeowner, organization=other_organization, role=Role.DISPATCHER
        )

        self._request_link(api_client, homeowner.email)

        assert not MagicLinkToken.objects.exists()

    def test_a_link_issued_before_a_promotion_no_longer_works(self, api_client, homeowner):
        token = self._token_for(api_client, homeowner)
        Membership.objects.filter(user=homeowner).update(role=Role.ADMIN)

        assert api_client.post(MAGIC_CONSUME_URL, {"token": token}).status_code == 401

    def test_a_deactivated_user_cannot_use_a_live_link(self, api_client, homeowner):
        token = self._token_for(api_client, homeowner)
        homeowner.is_active = False
        homeowner.save()

        assert api_client.post(MAGIC_CONSUME_URL, {"token": token}).status_code == 401

    def test_a_link_works_only_once(self, api_client, homeowner):
        token = self._token_for(api_client, homeowner)
        api_client.post(MAGIC_CONSUME_URL, {"token": token})
        api_client.post(reverse("users:logout"))

        assert api_client.post(MAGIC_CONSUME_URL, {"token": token}).status_code == 401

    def test_an_expired_link_is_refused(self, api_client, homeowner):
        from datetime import timedelta

        from django.utils import timezone

        token = self._token_for(api_client, homeowner)
        MagicLinkToken.objects.update(expires_at=timezone.now() - timedelta(minutes=1))

        assert api_client.post(MAGIC_CONSUME_URL, {"token": token}).status_code == 401

    def test_a_bogus_token_is_refused(self, api_client):
        assert api_client.post(MAGIC_CONSUME_URL, {"token": "nonsense"}).status_code == 401

    def test_the_raw_token_is_never_stored(self, api_client, homeowner):
        token = self._token_for(api_client, homeowner)

        assert not MagicLinkToken.objects.filter(token_hash=token).exists()
        assert MagicLinkToken.objects.count() == 1


@pytest.mark.django_db
class TestOrganizationEndpoint:
    def test_member_can_read_their_organization(self, authed_client, organization):
        response = authed_client.get(reverse("organizations:current"))

        assert response.status_code == 200
        assert response.json()["name"] == organization.name

    def test_owner_can_update_it(self, authed_client):
        response = authed_client.patch(
            reverse("organizations:current"), {"phone": "555-0100"}, format="json"
        )

        assert response.status_code == 200
        assert response.json()["phone"] == "555-0100"

    def test_an_invalid_timezone_is_rejected(self, authed_client):
        response = authed_client.patch(
            reverse("organizations:current"), {"timezone": "Mars/Olympus"}, format="json"
        )

        assert response.status_code == 400

    def test_a_cleaner_cannot_update_it(self, api_client, organization, make_member):
        cleaner = make_member(organization, role=Role.CLEANER)
        api_client.force_login(cleaner)

        response = api_client.patch(
            reverse("organizations:current"), {"phone": "555-0100"}, format="json"
        )

        assert response.status_code == 403

    def test_anonymous_is_rejected(self, api_client):
        assert api_client.get(reverse("organizations:current")).status_code == 403
