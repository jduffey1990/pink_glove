"""
Team management in an organization that has gone read-only.

Phase 4c (ADR-028) makes a lapsed or deactivated organization read-only:
`IsOrgMember` refuses every unsafe request with one sentence. Team
management sits on both sides of that line, so each side is pinned here.

The team's writes are writes like any other and are refused. But the ways
*in* are not the organization's to refuse: an owner who cannot sign in, reset
a password or accept an invitation cannot reach the page where they pay, and
a read-only organization would have become a locked one (ADR-034).
"""

import re

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from app.middleware.tenant import ORGANIZATION_HEADER
from base.permissions import ORGANIZATION_INACTIVE
from users.enums import Role
from users.models import Membership
from users.tests.team import (
    ACCEPT,
    GOOD_PASSWORD,
    INSPECT,
    INVITE_OPTIONS,
    LIST,
    SESSION,
    STAFF,
    action,
    client_for,
    detail,
    invite,
    member,
    token_of,
)

pytestmark = pytest.mark.django_db

LOGIN = reverse("two_factor:login")
VERIFY = reverse("two_factor:verify")
RESET = reverse("two_factor:password-reset")
RESET_CONFIRM = reverse("two_factor:password-reset-confirm")
OLD_PASSWORD = "pw-for-tests-only"  # noqa: S105


@pytest.fixture
def team(organization):
    return {role: member(organization, role) for role in STAFF}


@pytest.fixture
def read_only(organization, team):
    """Switched off after the team exists, as a lapse would find it."""
    organization.is_active = False
    organization.save()
    return organization


class TestTheTeamsWritesAreRefusedLikeAnyOtherWrite:
    def _refused(self, response):
        assert response.status_code == 403
        assert response.json() == {"detail": ORGANIZATION_INACTIVE}

    @pytest.mark.parametrize("caller", [Role.OWNER, Role.ADMIN])
    def test_inviting(self, team, read_only, caller):
        self._refused(invite(client_for(team[caller])))

        assert not Membership.objects.filter(user__email="new.person@example.com").exists()
        assert mail.outbox == []

    def test_changing_a_role(self, team, read_only):
        response = client_for(team[Role.OWNER]).patch(
            detail(team[Role.CLEANER]), {"role": Role.DISPATCHER}, format="json"
        )

        self._refused(response)
        team[Role.CLEANER].refresh_from_db()
        assert team[Role.CLEANER].role == Role.CLEANER

    def test_deactivating(self, team, read_only):
        self._refused(client_for(team[Role.OWNER]).post(action(team[Role.CLEANER], "deactivate")))

        team[Role.CLEANER].refresh_from_db()
        assert team[Role.CLEANER].is_active is True

    def test_reactivating(self, team, read_only):
        Membership.objects.filter(pk=team[Role.CLEANER].pk).update(
            is_active=False, deactivated_at=timezone.now()
        )

        self._refused(client_for(team[Role.OWNER]).post(action(team[Role.CLEANER], "reactivate")))

        team[Role.CLEANER].refresh_from_db()
        assert team[Role.CLEANER].is_active is False

    def test_resending_an_invitation(self, team, organization):
        owner = client_for(team[Role.OWNER])
        invited = Membership.objects.get(pk=invite(owner).json()["id"])
        organization.is_active = False
        organization.save()
        mail.outbox.clear()

        self._refused(owner.post(action(invited, "resend-invite")))

        assert mail.outbox == []

    def test_a_superuser_can_still_put_things_right(self, api_client, superuser, team, read_only):
        """`IsOrgMember`'s own exemption: someone has to be able to."""
        api_client.force_login(superuser)
        headers = {ORGANIZATION_HEADER.lower(): str(read_only.id)}

        response = api_client.post(action(team[Role.CLEANER], "deactivate"), headers=headers)

        assert response.status_code == 200


class TestTheTeamCanStillBeRead:
    def test_the_list_is_served(self, team, read_only):
        response = client_for(team[Role.OWNER]).get(LIST)

        assert response.status_code == 200
        assert response.json()["count"] == len(STAFF)

    @pytest.mark.parametrize("caller", [Role.OWNER, Role.ADMIN])
    def test_no_row_offers_an_action_the_server_would_refuse(self, team, read_only, caller):
        """
        `available_actions` is a promise about what the next request will be
        allowed to do. In a read-only organization that is nothing.
        """
        rows = client_for(team[caller]).get(LIST).json()["results"]

        assert rows
        assert [row["available_actions"] for row in rows] == [[]] * len(rows)
        assert [row["assignable_roles"] for row in rows] == [[]] * len(rows)

    def test_a_deactivated_row_does_not_offer_reactivate_either(self, team, read_only):
        Membership.objects.filter(pk=team[Role.CLEANER].pk).update(
            is_active=False, deactivated_at=timezone.now()
        )

        row = client_for(team[Role.OWNER]).get(detail(team[Role.CLEANER])).json()

        assert row["state"] == "deactivated"
        assert row["available_actions"] == []

    @pytest.mark.parametrize("caller", [Role.OWNER, Role.ADMIN])
    def test_nobody_is_offered_a_role_to_invite(self, team, read_only, caller):
        assert client_for(team[caller]).get(INVITE_OPTIONS).json() == {"roles": []}

    def test_a_superuser_is_still_offered_what_they_may_do(
        self, api_client, superuser, team, read_only
    ):
        """The exemption runs both ways: what is offered is what is accepted."""
        api_client.force_login(superuser)
        headers = {ORGANIZATION_HEADER.lower(): str(read_only.id)}

        row = api_client.get(detail(team[Role.CLEANER]), headers=headers).json()
        options = api_client.get(INVITE_OPTIONS, headers=headers).json()

        assert set(row["available_actions"]) == {"change_role", "deactivate"}
        assert options["roles"] == list(STAFF)

    def test_everything_is_offered_again_once_the_organization_is_back(self, team, read_only):
        read_only.is_active = True
        read_only.save()

        row = client_for(team[Role.OWNER]).get(detail(team[Role.CLEANER])).json()

        assert set(row["available_actions"]) == {"change_role", "deactivate"}

    def test_a_members_state_is_theirs_not_the_organizations(self, team, read_only):
        """
        Two fields that share words. The organization is deactivated; the
        people in it are not, and the team page must not say they are.
        """
        rows = client_for(team[Role.OWNER]).get(LIST).json()["results"]

        assert {row["state"] for row in rows} == {"active"}
        assert {row["is_active"] for row in rows} == {True}
        assert {row["organization"]["is_active"] for row in rows} == {False}


class TestNobodyIsLockedOutOfAReadOnlyOrganization:
    """The owner has to get in to pay. None of these is the organization's to refuse."""

    @pytest.fixture(autouse=True)
    def _local(self, settings):
        settings.LOCAL = True

    @pytest.mark.parametrize("role", STAFF)
    def test_everyone_can_still_sign_in(self, api_client, team, read_only, role):
        user = team[role].user

        login = api_client.post(
            LOGIN, {"email": user.email, "password": OLD_PASSWORD}, format="json"
        )
        assert login.status_code == 202
        verify = api_client.post(VERIFY, {"code": login.json()["dev_code"]}, format="json")
        assert verify.status_code == 200

        session = api_client.get(SESSION).json()
        assert session["current_role"] == role
        assert session["current_organization"]["id"] == str(read_only.id)
        assert session["current_organization"]["is_active"] is False

    def test_the_owner_can_ask_for_a_password_reset(self, api_client, team, read_only, settings):
        settings.LOCAL = False

        response = api_client.post(RESET, {"email": team[Role.OWNER].user.email}, format="json")

        assert response.status_code == 202
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [team[Role.OWNER].user.email]

    def test_the_owner_can_finish_one_and_then_sign_in(self, api_client, team, read_only):
        owner = team[Role.OWNER].user
        link = api_client.post(RESET, {"email": owner.email}, format="json").json()["dev_link"]

        confirm = api_client.post(
            RESET_CONFIRM, {"token": token_of(link), "password": GOOD_PASSWORD}, format="json"
        )
        assert confirm.status_code == 200

        login = api_client.post(LOGIN, {"email": owner.email, "password": GOOD_PASSWORD})
        assert login.status_code == 202
        api_client.post(VERIFY, {"code": login.json()["dev_code"]}, format="json")
        assert api_client.get(SESSION).json()["current_role"] == Role.OWNER

    def test_someone_invited_before_it_lapsed_can_still_accept(
        self, api_client, team, organization
    ):
        """A second owner invited to take over the bill, for one."""
        link = invite(client_for(team[Role.OWNER]), role=Role.OWNER).json()["dev_link"]
        organization.is_active = False
        organization.save()
        token = token_of(link)

        inspect = api_client.post(INSPECT, {"token": token}, format="json")
        accept = api_client.post(
            ACCEPT,
            {"token": token, "password": GOOD_PASSWORD, "first_name": "Maria"},
            format="json",
        )

        assert inspect.status_code == 200
        assert accept.status_code == 200
        membership = Membership.objects.get(user__email="new.person@example.com")
        assert membership.is_active is True
        assert membership.role == Role.OWNER

    def test_and_then_sign_in_to_it(self, api_client, team, organization):
        link = invite(client_for(team[Role.OWNER]), role=Role.OWNER).json()["dev_link"]
        organization.is_active = False
        organization.save()
        api_client.post(
            ACCEPT,
            {"token": token_of(link), "password": GOOD_PASSWORD, "first_name": "Maria"},
            format="json",
        )

        login = api_client.post(
            LOGIN, {"email": "new.person@example.com", "password": GOOD_PASSWORD}, format="json"
        )
        api_client.post(VERIFY, {"code": login.json()["dev_code"]}, format="json")

        assert api_client.get(SESSION).json()["current_organization"]["id"] == str(organization.id)

    def test_the_reset_email_link_is_followable_when_not_local(
        self, api_client, team, read_only, settings
    ):
        """The deployed path end to end: worker, email, confirm."""
        settings.LOCAL = False
        owner = team[Role.OWNER].user
        api_client.post(RESET, {"email": owner.email}, format="json")
        token = re.search(r"/reset-password#token=([\w-]+)", mail.outbox[-1].body).group(1)

        confirm = api_client.post(
            RESET_CONFIRM, {"token": token, "password": GOOD_PASSWORD}, format="json"
        )

        owner.refresh_from_db()
        assert confirm.status_code == 200
        assert owner.check_password(GOOD_PASSWORD)
