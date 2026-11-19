from urllib.parse import parse_qs, urlsplit

import pytest
from django.core import mail
from django.urls import reverse

from app.middleware.tenant import ORGANIZATION_HEADER
from users.enums import Role
from users.models import MagicLinkToken, Membership

SESSION_URL = reverse("users:session")
JOBS_URL = reverse("scheduling:job-list")
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
        return _fragment_token(self._request_link(api_client, user.email).json()["dev_link"])

    def test_the_link_carries_its_token_in_the_fragment(self, api_client, homeowner, settings):
        """ADR-036, ADR-039: nothing of the token in the path or the query."""
        link = self._request_link(api_client, homeowner.email).json()["dev_link"]
        parts = urlsplit(link)

        assert link.startswith(f"{settings.FRONTEND_BASE_URL}/sign-in#token=")
        assert parts.path == "/sign-in"
        assert parts.query == ""
        assert _fragment_token(link)

    def test_the_token_is_taken_from_the_body_only(self, api_client, homeowner):
        token = self._token_for(api_client, homeowner)

        response = api_client.post(f"{MAGIC_CONSUME_URL}?token={token}", {})

        assert response.status_code == 400
        # Not spent by the refused attempt: the body form still redeems it.
        assert api_client.post(MAGIC_CONSUME_URL, {"token": token}).status_code == 200

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

    def test_a_link_from_one_customer_signs_in_that_customer_only(
        self, api_client, homeowner, make_member, organization
    ):
        neighbour = make_member(organization, role=Role.CUSTOMER, email="next-door@example.com")
        token = self._token_for(api_client, homeowner)
        self._token_for(api_client, neighbour)

        body = api_client.post(MAGIC_CONSUME_URL, {"token": token}).json()

        assert body["email"] == homeowner.email

    def test_a_bogus_token_is_refused(self, api_client):
        assert api_client.post(MAGIC_CONSUME_URL, {"token": "nonsense"}).status_code == 401

    def test_the_raw_token_is_never_stored(self, api_client, homeowner):
        token = self._token_for(api_client, homeowner)

        assert not MagicLinkToken.objects.filter(token_hash=token).exists()
        assert MagicLinkToken.objects.count() == 1


def _fragment_token(link: str) -> str:
    return parse_qs(urlsplit(link).fragment)["token"][0]


@pytest.mark.django_db
class TestMagicLinkEmail:
    """Deployed (LOCAL off): the link goes by email, never in the response."""

    @pytest.fixture(autouse=True)
    def _deployed_mode(self, settings):
        settings.LOCAL = False
        settings.FRONTEND_BASE_URL = "https://app.example.test"

    def test_the_email_carries_the_fragment_form(self, api_client, organization, make_member):
        homeowner = make_member(organization, role=Role.CUSTOMER, email="homeowner@example.com")

        response = api_client.post(MAGIC_LINK_URL, {"email": homeowner.email})

        assert response.status_code == 202
        assert "dev_link" not in response.json()
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == [homeowner.email]
        assert "https://app.example.test/sign-in#token=" in message.body
        assert "/sign-in/" not in message.body

    def test_the_emailed_token_signs_the_customer_in(self, api_client, organization, make_member):
        homeowner = make_member(organization, role=Role.CUSTOMER, email="homeowner@example.com")
        api_client.post(MAGIC_LINK_URL, {"email": homeowner.email})
        link = mail.outbox[0].body.split("Sign in here: ", 1)[1].split()[0]

        response = api_client.post(MAGIC_CONSUME_URL, {"token": _fragment_token(link)})

        assert response.status_code == 200
        assert response.json()["email"] == homeowner.email

    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.DISPATCHER, Role.CLEANER])
    def test_staff_get_the_same_202_and_no_email(self, api_client, organization, make_member, role):
        staff = make_member(organization, role=role)

        response = api_client.post(MAGIC_LINK_URL, {"email": staff.email})

        assert response.status_code == 202
        assert mail.outbox == []


@pytest.mark.django_db
class TestSignedInCustomerAcrossTenants:
    """
    What a link's session can read: its own visits in its own organization,
    and nothing of another tenant's, header or no header.
    """

    @pytest.fixture(autouse=True)
    def _local_mode(self, settings):
        settings.LOCAL = True

    @pytest.fixture
    def scene(self, organization, other_organization, make_member):
        from scheduling.tests.factories import CustomerFactory, JobFactory

        user = make_member(organization, role=Role.CUSTOMER, email="homeowner@example.com")
        mine = JobFactory(
            organization=organization,
            customer=CustomerFactory(organization=organization, user=user),
        )
        JobFactory(organization=organization)  # a neighbour's visit, same tenant
        JobFactory(organization=other_organization)
        return user, mine

    def _sign_in(self, api_client, user):
        link = api_client.post(MAGIC_LINK_URL, {"email": user.email}).json()["dev_link"]
        assert (
            api_client.post(MAGIC_CONSUME_URL, {"token": _fragment_token(link)}).status_code == 200
        )

    def test_the_customer_reads_only_their_own_visits(self, api_client, scene):
        user, mine = scene
        self._sign_in(api_client, user)

        results = api_client.get(JOBS_URL).json()["results"]

        assert [job["id"] for job in results] == [str(mine.id)]
        # The customer's own shape: no crew roster, no office notes.
        assert "assignments" not in results[0]
        assert "notes" not in results[0]

    def test_another_tenants_header_opens_nothing(self, api_client, scene, other_organization):
        user, _ = scene
        self._sign_in(api_client, user)

        response = api_client.get(
            JOBS_URL, headers={ORGANIZATION_HEADER.lower(): str(other_organization.id)}
        )

        assert response.status_code == 403

    def test_the_customer_cannot_read_invoices(self, api_client, scene):
        """Pinned so the customer home's scope is a decision, not an accident (ADR-039)."""
        user, _ = scene
        self._sign_in(api_client, user)

        assert api_client.get(reverse("billing:invoice-list")).status_code == 403


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
