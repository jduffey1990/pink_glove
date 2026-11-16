"""
Following an invitation link.

Public endpoints, so the token is everything: single-use, good only for the
invitation it was issued for, and worth nothing once the invitation is
withdrawn. Accepting sets up the account and signs nobody in.
"""

from datetime import timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.throttling import SimpleRateThrottle

from users.enums import AccountTokenPurpose, MembershipState, Role
from users.models import AccountToken, CustomUser, MagicLinkToken, Membership
from users.services import INVALID_INVITATION
from users.tests.team import (
    ACCEPT,
    GOOD_PASSWORD,
    INSPECT,
    SESSION,
    action,
    client_for,
    invite,
    member,
    token_of,
)

pytestmark = pytest.mark.django_db

LOGIN = reverse("two_factor:login")
VERIFY = reverse("two_factor:verify")
MAGIC_CONSUME = reverse("users:magic-link-consume")


@pytest.fixture(autouse=True)
def _local(settings):
    """LOCAL hands the link back as `dev_link`, which is how these learn it."""
    settings.LOCAL = True


@pytest.fixture
def inviter(organization):
    return client_for(member(organization, Role.OWNER))


def _invitation(inviter, email="new.person@example.com", role=Role.CLEANER):
    """Invite, returning (raw token, membership)."""
    body = invite(inviter, email=email, role=role).json()
    return token_of(body["dev_link"]), Membership.objects.get(pk=body["id"])


def _accept(client, token, **fields):
    return client.post(ACCEPT, {"token": token, **fields}, format="json")


def _claim(client, token, password=GOOD_PASSWORD, first_name="Maria", last_name="Lopez"):
    return _accept(client, token, password=password, first_name=first_name, last_name=last_name)


class TestInspect:
    def test_it_says_who_is_asking_and_what_is_needed(self, api_client, inviter, organization):
        token, _ = _invitation(inviter, role=Role.DISPATCHER)

        response = api_client.post(INSPECT, {"token": token}, format="json")

        assert response.status_code == 200
        assert response.json() == {
            "organization_name": organization.name,
            "email": "new.person@example.com",
            "role": "dispatcher",
            "must_set_password": True,
            "first_name": "",
            "last_name": "",
        }

    def test_an_account_with_a_password_is_not_asked_for_one(
        self, api_client, inviter, other_organization
    ):
        known = member(other_organization, Role.ADMIN)
        token, _ = _invitation(inviter, email=known.user.email)

        body = api_client.post(INSPECT, {"token": token}, format="json").json()

        assert body["must_set_password"] is False

    def test_looking_does_not_spend_the_link(self, api_client, inviter):
        token, _ = _invitation(inviter)

        api_client.post(INSPECT, {"token": token}, format="json")
        api_client.post(INSPECT, {"token": token}, format="json")

        assert _claim(api_client, token).status_code == 200

    def test_a_bad_link_gets_one_sentence(self, api_client):
        response = api_client.post(INSPECT, {"token": "nope"}, format="json")

        assert response.status_code == 400
        assert response.json() == {"detail": INVALID_INVITATION}


class TestAcceptingANewAccount:
    def test_it_sets_the_password_and_the_name_and_joins(self, api_client, inviter, organization):
        token, membership = _invitation(inviter)

        response = _claim(api_client, token)

        assert response.status_code == 200
        assert organization.name in response.json()["detail"]
        membership.refresh_from_db()
        user = membership.user
        assert membership.is_active is True
        assert membership.accepted_at is not None
        assert membership.state == MembershipState.ACTIVE
        assert user.check_password(GOOD_PASSWORD)
        assert (user.first_name, user.last_name) == ("Maria", "Lopez")

    def test_it_does_not_sign_them_in(self, api_client, inviter):
        token, _ = _invitation(inviter)

        response = _claim(api_client, token)

        assert response.json().keys() == {"detail"}
        assert api_client.get(SESSION).json() == {}
        assert api_client.get(reverse("users:membership-list")).status_code == 403

    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.DISPATCHER, Role.CLEANER])
    def test_they_then_sign_in_through_the_ordinary_challenge(self, api_client, inviter, role):
        """ADR-008 is untouched: a password, then a code. No bypass was added."""
        token, _ = _invitation(inviter, role=role)
        _claim(api_client, token)

        login = api_client.post(
            LOGIN, {"email": "new.person@example.com", "password": GOOD_PASSWORD}, format="json"
        )

        assert login.status_code == 202
        assert login.json()["two_factor_required"] is True
        assert api_client.get(SESSION).json() == {}

        verify = api_client.post(VERIFY, {"code": login.json()["dev_code"]}, format="json")
        assert verify.status_code == 200
        # Read on the next request: the tenant is resolved before the view runs,
        # so the verify response itself predates the session it just created.
        assert api_client.get(SESSION).json()["current_role"] == role

    def test_they_cannot_sign_in_before_accepting(self, api_client, inviter):
        _invitation(inviter)

        login = api_client.post(
            LOGIN, {"email": "new.person@example.com", "password": GOOD_PASSWORD}, format="json"
        )

        assert login.status_code == 401

    @pytest.mark.parametrize(
        ("password", "fragment"),
        [
            ("short1!", "too short"),
            ("password", "too common"),
            ("8675309867", "entirely numeric"),
            ("new.person", "too similar"),
        ],
    )
    def test_the_password_goes_through_the_validators(
        self, api_client, inviter, password, fragment
    ):
        token, membership = _invitation(inviter)

        response = _claim(api_client, token, password=password)

        assert response.status_code == 400
        assert fragment in " ".join(response.json()["password"])
        membership.refresh_from_db()
        assert membership.is_active is False
        assert membership.user.has_usable_password() is False

    def test_the_password_is_compared_with_the_name_being_set(self, api_client, inviter):
        """The name arrives in the same request, so the validator has to be told it."""
        token, _ = _invitation(inviter)

        response = _claim(api_client, token, password="Bartholomew1", first_name="Bartholomew")

        assert response.status_code == 400
        assert "too similar to the first name" in " ".join(response.json()["password"])

    def test_a_refused_password_does_not_cost_the_link(self, api_client, inviter):
        token, _ = _invitation(inviter)
        assert _claim(api_client, token, password="password").status_code == 400

        assert _claim(api_client, token).status_code == 200

    def test_a_refused_password_does_not_leave_a_name_behind(self, api_client, inviter):
        token, membership = _invitation(inviter)

        _claim(api_client, token, password="password")

        membership.user.refresh_from_db()
        assert membership.user.first_name == ""

    def test_a_first_name_and_a_password_are_required(self, api_client, inviter):
        token, membership = _invitation(inviter)

        response = _accept(api_client, token)

        assert response.status_code == 400
        assert set(response.json()) == {"first_name", "password"}
        membership.refresh_from_db()
        assert membership.is_active is False

    def test_a_customer_who_only_had_magic_links_sets_a_password(
        self, api_client, inviter, other_organization
    ):
        """Staff now, so the magic link is gone and a password takes its place."""
        from users.services import may_use_magic_link

        customer = CustomUser.objects.create_user(email="homeowner@example.com")
        Membership.objects.create(
            user=customer, organization=other_organization, role=Role.CUSTOMER
        )
        token, _ = _invitation(inviter, email=customer.email)
        assert may_use_magic_link(customer) is True

        assert _claim(api_client, token).status_code == 200

        customer.refresh_from_db()
        assert customer.check_password(GOOD_PASSWORD)
        assert may_use_magic_link(customer) is False


class TestAcceptingWithAnExistingAccount:
    @pytest.fixture
    def known(self, other_organization):
        return member(other_organization, Role.ADMIN).user

    def test_it_joins_without_a_password(self, api_client, inviter, known, organization):
        token, membership = _invitation(inviter, email=known.email)

        response = _accept(api_client, token)

        assert response.status_code == 200
        membership.refresh_from_db()
        assert membership.is_active is True
        assert membership.organization == organization

    def test_a_password_in_the_body_does_not_replace_theirs(self, api_client, inviter, known):
        """Whoever holds the link must not be able to take the account."""
        token, _ = _invitation(inviter, email=known.email)
        before = known.password

        response = _claim(api_client, token, password=GOOD_PASSWORD, first_name="Mallory")

        known.refresh_from_db()
        assert response.status_code == 200
        assert known.password == before
        assert known.check_password("pw-for-tests-only")
        assert known.first_name != "Mallory"

    def test_their_open_session_survives(self, inviter, known, api_client):
        theirs = client_for(known)
        token, _ = _invitation(inviter, email=known.email)

        _accept(api_client, token)

        assert theirs.get(SESSION).json()["email"] == known.email
        assert len(theirs.get(SESSION).json()["memberships"]) == 2


class TestTheTokenIsSingleUseAndBound:
    def test_it_works_once(self, api_client, inviter):
        token, _ = _invitation(inviter)
        assert _claim(api_client, token).status_code == 200

        again = _claim(api_client, token, password="another-Good-pw-44")

        assert again.status_code == 400
        assert again.json() == {"detail": INVALID_INVITATION}
        assert CustomUser.objects.get(email="new.person@example.com").check_password(GOOD_PASSWORD)

    def test_it_expires_after_seven_days(self, api_client, inviter):
        token, membership = _invitation(inviter)
        AccountToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        response = _claim(api_client, token)

        membership.refresh_from_db()
        assert response.status_code == 400
        assert response.json() == {"detail": INVALID_INVITATION}
        assert membership.is_active is False

    def test_it_is_worth_nothing_once_the_invitation_is_withdrawn(self, api_client, inviter):
        token, membership = _invitation(inviter)
        inviter.post(action(membership, "deactivate"))

        response = _claim(api_client, token)

        membership.refresh_from_db()
        assert response.status_code == 400
        assert membership.is_active is False

    def test_the_old_link_stays_dead_after_a_re_invite(self, api_client, inviter):
        token, membership = _invitation(inviter)
        inviter.post(action(membership, "deactivate"))
        inviter.post(action(membership, "reactivate"))

        assert _claim(api_client, token).status_code == 400

    def test_a_token_whose_membership_is_no_longer_an_invitation_is_refused(
        self, api_client, inviter
    ):
        """Belt and braces: even a live token cannot re-open a decided row."""
        token, membership = _invitation(inviter)
        Membership.objects.filter(pk=membership.pk).update(
            is_active=False, deactivated_at=timezone.now()
        )

        assert _claim(api_client, token).status_code == 400

    def test_it_is_refused_for_an_account_that_was_switched_off(self, api_client, inviter):
        token, membership = _invitation(inviter)
        CustomUser.objects.filter(pk=membership.user_id).update(is_active=False)

        assert _claim(api_client, token).status_code == 400

    def test_a_password_reset_token_is_not_an_invitation(self, api_client, organization):
        target = member(organization, Role.CLEANER)
        _, reset = AccountToken.issue(AccountTokenPurpose.PASSWORD_RESET, target.user)

        assert api_client.post(INSPECT, {"token": reset}, format="json").status_code == 400
        assert _claim(api_client, reset).status_code == 400
        assert target.user.check_password("pw-for-tests-only")

    def test_a_magic_link_token_is_not_an_invitation(self, api_client, organization):
        customer = member(organization, Role.CUSTOMER)
        _, magic = MagicLinkToken.issue(customer.user)

        assert _claim(api_client, magic).status_code == 400

    def test_an_invitation_is_not_a_magic_link(self, api_client, inviter):
        """The one that would matter most: an invitation must not sign anyone in."""
        token, _ = _invitation(inviter)

        response = api_client.post(MAGIC_CONSUME, {"token": token}, format="json")

        assert response.status_code == 401
        assert api_client.get(SESSION).json() == {}
        # And trying it did not spend the invitation.
        assert _claim(api_client, token).status_code == 200

    def test_every_refusal_reads_the_same(self, api_client, inviter):
        """Unknown, used, expired and withdrawn are one answer to a stranger."""
        used, _ = _invitation(inviter, email="used@example.com")
        _claim(api_client, used)
        withdrawn, membership = _invitation(inviter, email="withdrawn@example.com")
        inviter.post(action(membership, "deactivate"))

        bodies = [_claim(api_client, token).json() for token in ("unknown-token", used, withdrawn)]

        assert bodies == [{"detail": INVALID_INVITATION}] * 3


class TestNothingIsSentOrSaidThatShouldNotBe:
    def test_accepting_sends_no_email(self, api_client, inviter):
        token, _ = _invitation(inviter)
        mail.outbox.clear()

        _claim(api_client, token)

        assert mail.outbox == []

    def test_nothing_sensitive_is_logged(self, api_client, inviter, caplog):
        token, _ = _invitation(inviter)

        with caplog.at_level("DEBUG"):
            _claim(api_client, token)
            _claim(api_client, token)

        assert token not in caplog.text
        assert GOOD_PASSWORD not in caplog.text


class TestThrottles:
    @pytest.fixture(autouse=True)
    def _tight_rates(self, monkeypatch, settings):
        rates = dict(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
        rates.update({"invite_accept": "3/hour", "team_invite": "2/hour"})
        monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", rates)
        cache.clear()
        yield
        cache.clear()

    def test_the_public_endpoints_share_one_bucket_per_address(self, api_client):
        statuses = [
            api_client.post(INSPECT, {"token": "x"}, format="json").status_code,
            api_client.post(ACCEPT, {"token": "x"}, format="json").status_code,
            api_client.post(INSPECT, {"token": "x"}, format="json").status_code,
            api_client.post(ACCEPT, {"token": "x"}, format="json").status_code,
        ]

        assert statuses == [400, 400, 400, 429]

    def test_a_forged_forwarded_header_does_not_buy_a_fresh_bucket(self, api_client):
        statuses = [
            api_client.post(
                ACCEPT, {"token": "x"}, format="json", HTTP_X_FORWARDED_FOR=f"10.0.0.{n}"
            ).status_code
            for n in range(4)
        ]

        assert statuses[-1] == 429

    def test_invitations_are_limited_per_inviter(self, inviter, organization):
        statuses = [invite(inviter, email=f"crew-{n}@example.com").status_code for n in range(3)]

        assert statuses == [201, 201, 429]

    def test_resending_counts_against_the_same_limit(self, inviter):
        body = invite(inviter).json()
        membership = Membership.objects.get(pk=body["id"])

        statuses = [inviter.post(action(membership, "resend-invite")).status_code for _ in range(2)]

        assert statuses == [200, 429]

    def test_one_inviters_limit_is_not_anothers(self, inviter, organization):
        for n in range(3):
            invite(inviter, email=f"crew-{n}@example.com")
        other = client_for(member(organization, Role.ADMIN))

        assert invite(other, email="fresh@example.com").status_code == 201

    def test_reading_the_team_is_not_throttled(self, inviter):
        statuses = {inviter.get(reverse("users:membership-list")).status_code for _ in range(5)}

        assert statuses == {200}
