"""
The team page's writes: invite, change role, deactivate, reactivate, resend.

Every rule is tested from each role, on both sides of its line, because the
line is the feature: an owner manages anyone, an admin manages dispatchers
and cleaners, nobody else manages anybody, and nobody can lock themselves or
the organization out.
"""

import hashlib
import re

import pytest
from django.core import mail
from django.utils import timezone

from app.middleware.tenant import ORGANIZATION_HEADER
from users.enums import AccountTokenPurpose, MembershipState, Role, TeamAction
from users.models import AccountToken, CustomUser, Membership
from users.tests.team import (
    EVERY_ROLE,
    INVITE_OPTIONS,
    LIST,
    STAFF,
    action,
    client_for,
    detail,
    invite,
    member,
    token_of,
)

pytestmark = pytest.mark.django_db

#: (caller, role being handed out or acted on) -> allowed.
MAY_MANAGE = {
    (Role.OWNER, Role.OWNER): True,
    (Role.OWNER, Role.ADMIN): True,
    (Role.OWNER, Role.DISPATCHER): True,
    (Role.OWNER, Role.CLEANER): True,
    (Role.ADMIN, Role.OWNER): False,
    (Role.ADMIN, Role.ADMIN): False,
    (Role.ADMIN, Role.DISPATCHER): True,
    (Role.ADMIN, Role.CLEANER): True,
    **{(caller, role): False for caller in (Role.DISPATCHER, Role.CLEANER) for role in STAFF},
}
MANAGE_CASES = [(caller, role, allowed) for (caller, role), allowed in MAY_MANAGE.items()]


@pytest.fixture
def team(organization):
    """One of every role in Sparkle Clean, keyed by role."""
    return {role: member(organization, role) for role in EVERY_ROLE}


@pytest.fixture
def rival_team(other_organization):
    return {role: member(other_organization, role) for role in EVERY_ROLE}


def _colleague(organization, role):
    """A second person in `role`, so a caller is never acting on themselves."""
    return member(organization, role, email=f"second-{role}@example.com")


def _row(client, membership):
    return client.get(detail(membership)).json()


class TestInvite:
    @pytest.mark.parametrize(("caller", "role", "allowed"), MANAGE_CASES)
    def test_who_may_invite_whom(self, team, caller, role, allowed):
        response = invite(client_for(team[caller]), role=role)

        assert response.status_code == (201 if allowed else 403)
        assert Membership.objects.filter(user__email="new.person@example.com").exists() is allowed

    def test_a_customer_cannot_invite(self, team):
        assert invite(client_for(team[Role.CUSTOMER])).status_code == 403

    def test_nobody_signed_in_cannot_invite(self, api_client, team):
        assert invite(api_client).status_code == 403

    @pytest.mark.parametrize("caller", [Role.OWNER, Role.ADMIN])
    def test_a_customer_is_not_invited_here(self, team, caller):
        response = invite(client_for(team[caller]), role=Role.CUSTOMER)

        assert response.status_code == 400
        assert "role" in response.json()
        assert not CustomUser.objects.filter(email="new.person@example.com").exists()

    def test_it_creates_an_invitation_not_an_active_membership(self, team, organization):
        response = invite(client_for(team[Role.OWNER]), role=Role.DISPATCHER)

        membership = Membership.objects.get(user__email="new.person@example.com")
        assert membership.organization == organization
        assert membership.role == Role.DISPATCHER
        assert membership.is_active is False
        assert membership.invited_at is not None
        assert membership.accepted_at is None
        assert membership.state == MembershipState.INVITED
        assert response.json()["state"] == "invited"
        assert response.json()["id"] == str(membership.id)

    def test_the_new_account_has_no_password_and_no_name(self, team):
        invite(client_for(team[Role.OWNER]))

        user = CustomUser.objects.get(email="new.person@example.com")
        assert user.has_usable_password() is False
        assert (user.first_name, user.last_name) == ("", "")

    def test_the_organization_comes_from_the_tenant_not_the_body(
        self, team, organization, other_organization
    ):
        """Invariant 2. A forged `organization` lands nowhere."""
        response = invite(
            client_for(team[Role.OWNER]),
            organization=str(other_organization.id),
            organization_id=str(other_organization.id),
        )

        assert response.status_code == 201
        membership = Membership.objects.get(user__email="new.person@example.com")
        assert membership.organization == organization
        assert not Membership.objects.filter(
            organization=other_organization, user__email="new.person@example.com"
        ).exists()

    def test_the_body_cannot_make_the_membership_active(self, team):
        """Mass assignment: nothing but the address and the role is read."""
        response = invite(
            client_for(team[Role.OWNER]),
            is_active=True,
            accepted_at=timezone.now().isoformat(),
            user=str(team[Role.OWNER].user_id),
            state="active",
        )

        assert response.status_code == 201
        membership = Membership.objects.get(user__email="new.person@example.com")
        assert membership.is_active is False
        assert membership.accepted_at is None

    def test_the_address_is_case_insensitive(self, team):
        client = client_for(team[Role.OWNER])
        assert invite(client, email="New.Person@Example.COM").status_code == 201

        assert invite(client, email="new.person@example.com").status_code == 400
        assert CustomUser.objects.filter(email__iexact="new.person@example.com").count() == 1

    def test_a_bad_address_is_a_400(self, team):
        response = invite(client_for(team[Role.OWNER]), email="not-an-address")

        assert response.status_code == 400
        assert "email" in response.json()

    @pytest.mark.parametrize("role", EVERY_ROLE)
    def test_someone_already_here_is_not_invited_again(self, team, role):
        response = invite(client_for(team[Role.OWNER]), email=team[role].user.email)

        assert response.status_code == 400
        assert "email" in response.json()
        assert Membership.objects.filter(user=team[role].user).count() == 1

    def test_someone_deactivated_here_is_not_invited_again(self, team):
        Membership.objects.filter(pk=team[Role.CLEANER].pk).update(is_active=False)

        response = invite(client_for(team[Role.OWNER]), email=team[Role.CLEANER].user.email)

        assert response.status_code == 400
        assert "reactivate" in response.json()["email"][0]

    def test_a_membership_deleted_in_the_admin_comes_back_as_an_invitation(self, team):
        """Never as live access: the unique index would otherwise make it a 500."""
        gone = team[Role.DISPATCHER]
        gone.delete()

        response = invite(client_for(team[Role.OWNER]), email=gone.user.email, role=Role.CLEANER)

        assert response.status_code == 201
        gone.refresh_from_db()
        assert gone.deleted_at is None
        assert gone.is_active is False
        assert gone.role == Role.CLEANER
        assert gone.state == MembershipState.INVITED

    def test_a_deactivated_organization_cannot_invite(self, team, organization):
        organization.is_active = False
        organization.save()

        assert invite(client_for(team[Role.OWNER])).status_code == 403

    def test_a_superuser_acting_by_header_may_invite_an_owner(
        self, api_client, superuser, organization
    ):
        api_client.force_login(superuser)

        response = invite(
            api_client, role=Role.OWNER, headers={ORGANIZATION_HEADER.lower(): str(organization.id)}
        )

        assert response.status_code == 201
        assert Membership.objects.get(user__email="new.person@example.com").organization == (
            organization
        )


class TestInvitationEmail:
    def test_the_link_is_emailed_and_not_returned(self, team, settings):
        settings.FRONTEND_BASE_URL = "https://app.example.test"

        response = invite(client_for(team[Role.OWNER]))

        assert "dev_link" not in response.json()
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == ["new.person@example.com"]
        assert "Sparkle Clean" in message.subject
        assert "https://app.example.test/accept-invite/" in message.body
        assert "Cleaner" in message.body

    def test_the_link_comes_back_only_when_running_locally(self, team, settings):
        settings.LOCAL = True

        response = invite(client_for(team[Role.OWNER]))

        assert response.json()["dev_link"].startswith(
            f"{settings.FRONTEND_BASE_URL}/accept-invite/"
        )
        # Mirrors `dev_code`: locally the link is in the response, not a mailbox.
        assert mail.outbox == []

    def test_only_the_hash_is_stored(self, team, settings):
        settings.LOCAL = True

        raw = token_of(invite(client_for(team[Role.OWNER])).json()["dev_link"])

        token = AccountToken.objects.get()
        assert token.purpose == AccountTokenPurpose.INVITE
        assert token.token_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in token.token_hash
        assert token.membership.user.email == "new.person@example.com"

    def test_it_lasts_seven_days(self, team):
        invite(client_for(team[Role.OWNER]))

        token = AccountToken.objects.get()
        lifetime = token.expires_at - token.created_at
        assert abs(lifetime.total_seconds() - 7 * 24 * 3600) < 5

    def test_a_mail_failure_is_logged_without_the_link(self, team, monkeypatch, caplog):
        def boom(self, fail_silently=False):
            raise OSError("smtp is down")

        monkeypatch.setattr("django.core.mail.EmailMessage.send", boom)

        with caplog.at_level("DEBUG"):
            response = invite(client_for(team[Role.OWNER]))

        # The membership stands; Resend is the way out.
        assert response.status_code == 201
        assert "Failed to send invitation" in caplog.text
        assert "accept-invite" not in caplog.text
        assert "new.person@example.com" not in caplog.text

    def test_nothing_about_the_link_is_logged_on_success(self, team, caplog):
        with caplog.at_level("DEBUG"):
            invite(client_for(team[Role.OWNER]))

        raw = re.search(r"accept-invite/([\w-]+)", mail.outbox[0].body).group(1)
        assert raw not in caplog.text
        assert "accept-invite" not in caplog.text


class TestInvitingSomeoneWhoAlreadyHasAnAccount:
    """
    The address belongs to a user of another organization. They get a
    membership here and nothing else changes, and the inviter learns nothing.
    """

    def _strip(self, body):
        return {
            key: value
            for key, value in body.items()
            if key not in ("id", "user", "user_email", "user_name", "invited_at")
        }

    def test_the_response_is_the_same_as_for_a_stranger(self, team, rival_team):
        client = client_for(team[Role.OWNER])

        stranger = invite(client, email="stranger@example.com")
        known = invite(client, email=rival_team[Role.ADMIN].user.email)

        assert known.status_code == stranger.status_code == 201
        assert self._strip(known.json()) == self._strip(stranger.json())
        assert known.json().keys() == stranger.json().keys()

    def test_their_name_is_not_shown_until_they_accept(self, team, rival_team):
        """A name would say the address already has an account, and whose."""
        known = rival_team[Role.ADMIN].user
        client = client_for(team[Role.OWNER])

        body = invite(client, email=known.email).json()

        assert body["user_name"] == known.email
        assert known.full_name not in str(body)
        listed = [row for row in client.get(LIST).json()["results"] if row["id"] == body["id"]]
        assert listed[0]["user_name"] == known.email

    def test_their_account_is_not_written_to(self, team, rival_team):
        known = rival_team[Role.ADMIN].user
        before = (known.password, known.first_name, known.last_name, known.updated_at)

        invite(client_for(team[Role.OWNER]), email=known.email)

        known.refresh_from_db()
        assert (known.password, known.first_name, known.last_name, known.updated_at) == before

    def test_their_other_membership_is_untouched(self, team, rival_team, other_organization):
        theirs = rival_team[Role.ADMIN]

        invite(client_for(team[Role.OWNER]), email=theirs.user.email)

        theirs.refresh_from_db()
        assert theirs.is_active is True
        assert theirs.role == Role.ADMIN
        assert theirs.organization == other_organization

    def test_an_unaccepted_invitation_is_not_an_organization_they_can_act_in(
        self, team, rival_team, organization, other_organization
    ):
        theirs = rival_team[Role.ADMIN]
        invite(client_for(team[Role.OWNER]), email=theirs.user.email, role=Role.ADMIN)
        client = client_for(theirs)

        session = client.get("/api/users/session/").json()
        named = client.get(LIST, headers={ORGANIZATION_HEADER.lower(): str(organization.id)})

        # Still exactly one organization, resolved without a header as before.
        assert [row["organization"]["id"] for row in session["memberships"]] == [
            str(other_organization.id)
        ]
        assert session["current_organization"]["id"] == str(other_organization.id)
        assert named.status_code == 403

    def test_inviting_a_rivals_customer_does_not_take_their_magic_link_away(self, team, rival_team):
        """
        The reason an invitation starts inactive. Counted as a membership, a
        staff invitation would make this customer "not only a customer", and
        any organization's admin could lock a rival's customers out by
        inviting them.
        """
        from users.services import may_use_magic_link

        customer = rival_team[Role.CUSTOMER].user
        assert may_use_magic_link(customer) is True

        invite(client_for(team[Role.ADMIN]), email=customer.email, role=Role.CLEANER)

        assert may_use_magic_link(customer) is True

    def test_inviting_a_rivals_cleaner_does_not_change_their_2fa(self, team, rival_team):
        """An owner invitation must not start challenging them before they accept."""
        from two_factor.models import TrustedDevice
        from two_factor.services import two_factor_required

        cleaner = rival_team[Role.CLEANER].user
        _, device = TrustedDevice.issue(cleaner)
        assert two_factor_required(cleaner, device) is False

        invite(client_for(team[Role.OWNER]), email=cleaner.email, role=Role.OWNER)

        assert two_factor_required(cleaner, device) is False


class TestWhatEachRowOffers:
    """`available_actions` is the caller's answer, not a property of the row."""

    def _offers(self, caller, target):
        row = _row(client_for(caller), target)
        return set(row["available_actions"]), set(row["assignable_roles"])

    def test_an_owner_is_offered_everything_on_an_admin(self, team):
        actions, roles = self._offers(team[Role.OWNER], team[Role.ADMIN])

        assert actions == {TeamAction.CHANGE_ROLE, TeamAction.DEACTIVATE}
        assert roles == {Role.OWNER, Role.DISPATCHER, Role.CLEANER}

    def test_nobody_is_offered_anything_on_themselves(self, team, organization):
        _colleague(organization, Role.OWNER)

        for role in STAFF:
            assert self._offers(team[role], team[role]) == (set(), set())

    @pytest.mark.parametrize("target", [Role.OWNER, Role.ADMIN])
    def test_an_admin_is_offered_nothing_on_an_owner_or_another_admin(
        self, team, organization, target
    ):
        other = _colleague(organization, target)

        assert self._offers(team[Role.ADMIN], other) == (set(), set())

    def test_an_admin_is_offered_only_the_lower_tiers(self, team):
        actions, roles = self._offers(team[Role.ADMIN], team[Role.DISPATCHER])

        assert actions == {TeamAction.CHANGE_ROLE, TeamAction.DEACTIVATE}
        assert roles == {Role.CLEANER}

    @pytest.mark.parametrize("caller", [Role.DISPATCHER, Role.CLEANER])
    def test_below_admin_nothing_is_offered_on_anyone(self, team, caller):
        client = client_for(team[caller])

        rows = client.get(LIST).json()["results"]

        assert rows
        assert all(row["available_actions"] == [] for row in rows)
        assert all(row["assignable_roles"] == [] for row in rows)

    def test_a_customer_row_offers_nothing_even_to_an_owner(self, team):
        assert self._offers(team[Role.OWNER], team[Role.CUSTOMER]) == (set(), set())

    def test_an_invitation_offers_resend(self, team):
        client = client_for(team[Role.OWNER])
        invited = Membership.objects.get(pk=invite(client).json()["id"])

        actions, _ = self._offers(team[Role.OWNER], invited)

        assert actions == {
            TeamAction.CHANGE_ROLE,
            TeamAction.DEACTIVATE,
            TeamAction.RESEND_INVITE,
        }

    def test_a_deactivated_row_offers_only_reactivate(self, team):
        Membership.objects.filter(pk=team[Role.CLEANER].pk).update(
            is_active=False, deactivated_at=timezone.now()
        )

        actions, roles = self._offers(team[Role.OWNER], team[Role.CLEANER])

        assert actions == {TeamAction.REACTIVATE}
        assert roles == set()

    def test_the_last_owner_cannot_be_demoted_or_deactivated_by_anyone(
        self, api_client, superuser, team, organization
    ):
        api_client.force_login(superuser)
        headers = {ORGANIZATION_HEADER.lower(): str(organization.id)}

        row = api_client.get(detail(team[Role.OWNER]), headers=headers).json()

        assert row["available_actions"] == []
        assert row["assignable_roles"] == []

    def test_with_two_owners_either_may_be_managed(self, team, organization):
        second = _colleague(organization, Role.OWNER)

        actions, roles = self._offers(team[Role.OWNER], second)

        assert actions == {TeamAction.CHANGE_ROLE, TeamAction.DEACTIVATE}
        assert roles == {Role.ADMIN, Role.DISPATCHER, Role.CLEANER}

    def test_the_state_is_published(self, team):
        client = client_for(team[Role.OWNER])
        invited_id = invite(client).json()["id"]
        Membership.objects.filter(pk=team[Role.CLEANER].pk).update(is_active=False)

        states = {row["id"]: row["state"] for row in client.get(LIST).json()["results"]}

        assert states[invited_id] == "invited"
        assert states[str(team[Role.CLEANER].id)] == "deactivated"
        assert states[str(team[Role.ADMIN].id)] == "active"

    def test_the_staff_filter_leaves_the_customers_out(self, team):
        client = client_for(team[Role.OWNER])

        staff = client.get(LIST, {"staff": "true"}).json()["results"]
        customers = client.get(LIST, {"staff": "false"}).json()["results"]

        assert {row["role"] for row in staff} == set(STAFF)
        assert {row["role"] for row in customers} == {Role.CUSTOMER}

    def test_the_filter_cannot_show_a_cleaner_the_customers(self, team):
        """It narrows what the caller may already see. It never widens it."""
        client = client_for(team[Role.CLEANER])

        assert client.get(LIST, {"staff": "false"}).json()["results"] == []
        assert {row["role"] for row in client.get(LIST, {"staff": "true"}).json()["results"]} == (
            set(STAFF)
        )

    def test_the_list_costs_the_same_however_long_it_is(
        self, team, organization, django_assert_max_num_queries
    ):
        client = client_for(team[Role.OWNER])
        for n in range(12):
            member(organization, Role.CLEANER, email=f"crew-{n}@example.com")

        with django_assert_max_num_queries(12):
            assert client.get(LIST).json()["count"] == 17


class TestInviteOptions:
    def test_an_owner_may_invite_any_staff_role(self, team):
        body = client_for(team[Role.OWNER]).get(INVITE_OPTIONS).json()

        assert body == {"roles": list(STAFF)}

    def test_an_admin_may_invite_the_lower_tiers(self, team):
        body = client_for(team[Role.ADMIN]).get(INVITE_OPTIONS).json()

        assert body == {"roles": [Role.DISPATCHER, Role.CLEANER]}

    @pytest.mark.parametrize("caller", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_nobody_else_is_told(self, team, caller):
        assert client_for(team[caller]).get(INVITE_OPTIONS).status_code == 403


class TestChangeRole:
    def _patch(self, caller, target, role, **extra):
        return client_for(caller).patch(detail(target), {"role": role, **extra}, format="json")

    @pytest.mark.parametrize(("caller", "target_role", "allowed"), MANAGE_CASES)
    def test_who_may_change_whose_role(self, team, organization, caller, target_role, allowed):
        """The target's current tier decides whether the caller may touch them."""
        target = _colleague(organization, target_role)
        to_role = Role.CLEANER if target_role != Role.CLEANER else Role.DISPATCHER

        response = self._patch(team[caller], target, to_role)

        target.refresh_from_db()
        assert response.status_code == (200 if allowed else 403)
        assert target.role == (to_role if allowed else target_role)

    @pytest.mark.parametrize(("caller", "to_role", "allowed"), MANAGE_CASES)
    def test_who_may_grant_which_role(self, team, organization, caller, to_role, allowed):
        """And the tier being granted decides it again: no promoting past yourself."""
        start = Role.CLEANER if to_role != Role.CLEANER else Role.DISPATCHER
        target = _colleague(organization, start)

        response = self._patch(team[caller], target, to_role)

        target.refresh_from_db()
        assert response.status_code == (200 if allowed else 403)
        assert target.role == (to_role if allowed else start)

    def test_a_customer_cannot_change_anything(self, team):
        response = self._patch(team[Role.CUSTOMER], team[Role.CLEANER], Role.DISPATCHER)

        assert response.status_code == 403

    @pytest.mark.parametrize("caller", [Role.OWNER, Role.ADMIN])
    def test_nobody_changes_their_own_role(self, team, organization, caller):
        _colleague(organization, Role.OWNER)

        response = self._patch(team[caller], team[caller], Role.CLEANER)

        team[caller].refresh_from_db()
        assert response.status_code == 403
        assert team[caller].role == caller

    def test_an_admin_cannot_promote_themselves_to_owner(self, team):
        response = self._patch(team[Role.ADMIN], team[Role.ADMIN], Role.OWNER)

        team[Role.ADMIN].refresh_from_db()
        assert response.status_code == 403
        assert team[Role.ADMIN].role == Role.ADMIN

    def test_a_customer_membership_is_not_managed_here(self, team):
        response = self._patch(team[Role.OWNER], team[Role.CUSTOMER], Role.CLEANER)

        team[Role.CUSTOMER].refresh_from_db()
        assert response.status_code == 403
        assert team[Role.CUSTOMER].role == Role.CUSTOMER

    def test_nobody_is_made_a_customer_here(self, team):
        """Not a choice at all, so it is refused as a bad value, not as a rank."""
        response = self._patch(team[Role.OWNER], team[Role.CLEANER], Role.CUSTOMER)

        team[Role.CLEANER].refresh_from_db()
        assert response.status_code == 400
        assert "role" in response.json()
        assert team[Role.CLEANER].role == Role.CLEANER

    def test_a_made_up_role_is_a_400(self, team):
        response = self._patch(team[Role.OWNER], team[Role.CLEANER], "emperor")

        assert response.status_code == 400

    def test_a_patch_with_no_role_is_a_400(self, team):
        response = client_for(team[Role.OWNER]).patch(
            detail(team[Role.CLEANER]), {"is_active": False}, format="json"
        )

        team[Role.CLEANER].refresh_from_db()
        assert response.status_code == 400
        assert team[Role.CLEANER].is_active is True

    def test_the_body_changes_the_role_and_nothing_else(self, team, other_organization, rival_team):
        """Mass assignment: `user`, `organization` and `is_active` are not fields."""
        target = team[Role.CLEANER]

        response = self._patch(
            team[Role.OWNER],
            target,
            Role.DISPATCHER,
            is_active=False,
            user=str(rival_team[Role.OWNER].user_id),
            organization=str(other_organization.id),
            accepted_at=None,
            deactivated_at=timezone.now().isoformat(),
        )

        before_user = target.user_id
        target.refresh_from_db()
        assert response.status_code == 200
        assert target.role == Role.DISPATCHER
        assert target.is_active is True
        assert target.user_id == before_user
        assert target.organization_id == team[Role.OWNER].organization_id
        assert target.deactivated_at is None

    def test_the_last_owner_cannot_be_demoted(self, api_client, superuser, team, organization):
        """Only a superuser can even try: an owner cannot demote themselves."""
        api_client.force_login(superuser)

        response = api_client.patch(
            detail(team[Role.OWNER]),
            {"role": Role.ADMIN},
            format="json",
            headers={ORGANIZATION_HEADER.lower(): str(organization.id)},
        )

        team[Role.OWNER].refresh_from_db()
        assert response.status_code == 409
        assert team[Role.OWNER].role == Role.OWNER

    def test_an_owner_who_cannot_sign_in_does_not_count_as_one(
        self, api_client, superuser, team, organization
    ):
        ghost = _colleague(organization, Role.OWNER)
        CustomUser.objects.filter(pk=ghost.user_id).update(is_active=False)
        api_client.force_login(superuser)

        response = api_client.patch(
            detail(team[Role.OWNER]),
            {"role": Role.ADMIN},
            format="json",
            headers={ORGANIZATION_HEADER.lower(): str(organization.id)},
        )

        assert response.status_code == 409

    def test_one_of_two_owners_can_be_demoted(self, team, organization):
        second = _colleague(organization, Role.OWNER)

        response = self._patch(team[Role.OWNER], second, Role.ADMIN)

        second.refresh_from_db()
        assert response.status_code == 200
        assert second.role == Role.ADMIN

    def test_a_deactivated_person_is_reactivated_first(self, team):
        Membership.objects.filter(pk=team[Role.CLEANER].pk).update(is_active=False)

        response = self._patch(team[Role.OWNER], team[Role.CLEANER], Role.DISPATCHER)

        assert response.status_code == 409

    def test_an_invitations_role_can_be_changed_before_it_is_accepted(self, team):
        client = client_for(team[Role.OWNER])
        invited = Membership.objects.get(pk=invite(client).json()["id"])

        response = client.patch(detail(invited), {"role": Role.DISPATCHER}, format="json")

        invited.refresh_from_db()
        assert response.status_code == 200
        assert invited.role == Role.DISPATCHER
        assert invited.is_active is False

    def test_the_new_role_applies_to_an_open_session_at_once(self, team):
        """A demoted admin's next request is a dispatcher's."""
        demoted = client_for(team[Role.ADMIN])
        assert demoted.get(INVITE_OPTIONS).status_code == 200

        self._patch(team[Role.OWNER], team[Role.ADMIN], Role.DISPATCHER)

        assert demoted.get(INVITE_OPTIONS).status_code == 403

    @pytest.mark.parametrize("method", ["put", "delete"])
    def test_there_is_no_put_and_no_delete(self, team, method):
        client = client_for(team[Role.OWNER])

        response = getattr(client, method)(
            detail(team[Role.CLEANER]), {"role": Role.DISPATCHER}, format="json"
        )

        assert response.status_code == 405
        assert Membership.objects.filter(pk=team[Role.CLEANER].pk).exists()


class TestDeactivate:
    def _deactivate(self, caller, target):
        return client_for(caller).post(action(target, "deactivate"))

    @pytest.mark.parametrize(("caller", "target_role", "allowed"), MANAGE_CASES)
    def test_who_may_deactivate_whom(self, team, organization, caller, target_role, allowed):
        target = _colleague(organization, target_role)

        response = self._deactivate(team[caller], target)

        target.refresh_from_db()
        assert response.status_code == (200 if allowed else 403)
        assert target.is_active is (not allowed)
        assert (target.deactivated_at is not None) is allowed

    def test_a_customer_cannot_deactivate_anyone(self, team):
        assert self._deactivate(team[Role.CUSTOMER], team[Role.CLEANER]).status_code == 403

    @pytest.mark.parametrize("caller", [Role.OWNER, Role.ADMIN])
    def test_nobody_deactivates_themselves(self, team, organization, caller):
        _colleague(organization, Role.OWNER)

        response = self._deactivate(team[caller], team[caller])

        team[caller].refresh_from_db()
        assert response.status_code == 403
        assert team[caller].is_active is True

    def test_a_customer_membership_is_not_deactivated_here(self, team):
        response = self._deactivate(team[Role.OWNER], team[Role.CUSTOMER])

        team[Role.CUSTOMER].refresh_from_db()
        assert response.status_code == 403
        assert team[Role.CUSTOMER].is_active is True

    def test_the_last_owner_cannot_be_deactivated(self, api_client, superuser, team, organization):
        api_client.force_login(superuser)

        response = api_client.post(
            action(team[Role.OWNER], "deactivate"),
            headers={ORGANIZATION_HEADER.lower(): str(organization.id)},
        )

        team[Role.OWNER].refresh_from_db()
        assert response.status_code == 409
        assert team[Role.OWNER].is_active is True

    def test_an_invited_owner_does_not_count_as_one(
        self, api_client, superuser, team, organization
    ):
        """Someone who has not accepted cannot run the organization."""
        invite(client_for(team[Role.OWNER]), role=Role.OWNER)
        api_client.force_login(superuser)

        response = api_client.post(
            action(team[Role.OWNER], "deactivate"),
            headers={ORGANIZATION_HEADER.lower(): str(organization.id)},
        )

        assert response.status_code == 409

    def test_once_one_of_two_owners_is_gone_the_other_is_the_last(
        self, api_client, superuser, team, organization
    ):
        second = _colleague(organization, Role.OWNER)
        assert self._deactivate(team[Role.OWNER], second).status_code == 200
        api_client.force_login(superuser)

        response = api_client.post(
            action(team[Role.OWNER], "deactivate"),
            headers={ORGANIZATION_HEADER.lower(): str(organization.id)},
        )

        assert response.status_code == 409

    def test_deactivating_twice_is_a_409(self, team):
        self._deactivate(team[Role.OWNER], team[Role.CLEANER])

        assert self._deactivate(team[Role.OWNER], team[Role.CLEANER]).status_code == 409

    def test_the_person_is_refused_on_their_next_request(self, team):
        """Through the API this time, and with the session already open."""
        cleaner = client_for(team[Role.CLEANER])
        assert cleaner.get(LIST).status_code == 200

        self._deactivate(team[Role.OWNER], team[Role.CLEANER])

        assert cleaner.get(LIST).status_code == 403
        assert cleaner.get("/api/users/session/").json()["memberships"] == []

    def test_a_deactivated_admin_cannot_manage_anyone(self, team):
        admin = client_for(team[Role.ADMIN])

        self._deactivate(team[Role.OWNER], team[Role.ADMIN])

        assert admin.post(action(team[Role.CLEANER], "deactivate")).status_code == 403
        assert invite(admin).status_code == 403

    def test_it_withdraws_a_pending_invitation(self, team, settings):
        settings.LOCAL = True
        client = client_for(team[Role.OWNER])
        body = invite(client).json()
        invited = Membership.objects.get(pk=body["id"])

        response = client.post(action(invited, "deactivate"))

        assert response.status_code == 200
        assert response.json()["state"] == "deactivated"
        assert AccountToken.objects.filter(consumed_at__isnull=True).count() == 0


class TestReactivate:
    def _reactivate(self, caller, target):
        return client_for(caller).post(action(target, "reactivate"))

    def _switched_off(self, organization, role, **fields):
        return member(
            organization,
            role,
            email=f"off-{role}@example.com",
            is_active=False,
            deactivated_at=timezone.now(),
            **fields,
        )

    @pytest.mark.parametrize(("caller", "target_role", "allowed"), MANAGE_CASES)
    def test_who_may_reactivate_whom(self, team, organization, caller, target_role, allowed):
        target = self._switched_off(organization, target_role)

        response = self._reactivate(team[caller], target)

        target.refresh_from_db()
        assert response.status_code == (200 if allowed else 403)
        assert target.is_active is allowed

    def test_someone_who_was_working_here_is_active_again_at_once(self, team, organization):
        target = self._switched_off(organization, Role.CLEANER, accepted_at=timezone.now())

        response = self._reactivate(team[Role.OWNER], target)

        target.refresh_from_db()
        assert response.json()["state"] == "active"
        assert target.is_active is True
        assert target.deactivated_at is None
        assert mail.outbox == []

    def test_a_membership_switched_off_in_the_admin_comes_back_too(self, team, organization):
        """No `deactivated_at`, no invitation: an ordinary membership, inactive."""
        target = member(organization, Role.CLEANER, email="legacy@example.com", is_active=False)
        assert target.state == MembershipState.DEACTIVATED

        response = self._reactivate(team[Role.OWNER], target)

        target.refresh_from_db()
        assert response.status_code == 200
        assert target.is_active is True

    def test_a_withdrawn_invitation_becomes_an_invitation_again_not_access(self, team):
        """
        Invite, deactivate, reactivate must not add up to a membership nobody
        agreed to.
        """
        client = client_for(team[Role.OWNER])
        invited = Membership.objects.get(pk=invite(client).json()["id"])
        client.post(action(invited, "deactivate"))
        mail.outbox.clear()

        response = client.post(action(invited, "reactivate"))

        invited.refresh_from_db()
        assert response.status_code == 200
        assert response.json()["state"] == "invited"
        assert invited.is_active is False
        assert invited.accepted_at is None
        # A fresh link, because the old one was withdrawn with the invitation.
        assert len(mail.outbox) == 1
        assert AccountToken.objects.filter(consumed_at__isnull=True).count() == 1

    def test_someone_active_is_a_409(self, team):
        assert self._reactivate(team[Role.OWNER], team[Role.CLEANER]).status_code == 409

    def test_a_customer_cannot_reactivate_anyone(self, team, organization):
        target = self._switched_off(organization, Role.CLEANER)

        assert self._reactivate(team[Role.CUSTOMER], target).status_code == 403


class TestResendInvite:
    def _invited(self, client, role=Role.CLEANER, email="new.person@example.com"):
        body = invite(client, email=email, role=role).json()
        return Membership.objects.get(pk=body["id"]), body

    @pytest.mark.parametrize(("caller", "target_role", "allowed"), MANAGE_CASES)
    def test_who_may_resend_whose_invitation(self, team, caller, target_role, allowed):
        invited, _ = self._invited(client_for(team[Role.OWNER]), role=target_role)
        mail.outbox.clear()

        response = client_for(team[caller]).post(action(invited, "resend-invite"))

        assert response.status_code == (200 if allowed else 403)
        assert len(mail.outbox) == (1 if allowed else 0)

    def test_a_customer_cannot_resend(self, team):
        invited, _ = self._invited(client_for(team[Role.OWNER]))

        response = client_for(team[Role.CUSTOMER]).post(action(invited, "resend-invite"))

        assert response.status_code == 403

    def test_the_earlier_link_stops_working(self, team, settings, api_client):
        from users.tests.team import INSPECT

        settings.LOCAL = True
        client = client_for(team[Role.OWNER])
        invited, body = self._invited(client)
        first = token_of(body["dev_link"])

        second = token_of(client.post(action(invited, "resend-invite")).json()["dev_link"])

        assert first != second
        assert api_client.post(INSPECT, {"token": first}, format="json").status_code == 400
        assert api_client.post(INSPECT, {"token": second}, format="json").status_code == 200
        assert AccountToken.objects.filter(consumed_at__isnull=True).count() == 1

    def test_it_restarts_the_seven_days(self, team):
        client = client_for(team[Role.OWNER])
        invited, _ = self._invited(client)
        Membership.objects.filter(pk=invited.pk).update(
            invited_at=timezone.now() - timezone.timedelta(days=30)
        )

        client.post(action(invited, "resend-invite"))

        invited.refresh_from_db()
        assert (timezone.now() - invited.invited_at).total_seconds() < 60

    def test_there_is_nothing_to_resend_once_accepted(self, team):
        response = client_for(team[Role.OWNER]).post(action(team[Role.CLEANER], "resend-invite"))

        assert response.status_code == 409
        assert mail.outbox == []


class TestAnotherOrganizationsMemberships:
    """A membership id from another tenant is a 404 for every verb."""

    @pytest.mark.parametrize("role", STAFF)
    def test_they_cannot_be_read(self, team, rival_team, role):
        assert client_for(team[Role.OWNER]).get(detail(rival_team[role])).status_code == 404

    def test_they_are_not_listed(self, team, rival_team):
        rows = client_for(team[Role.OWNER]).get(LIST).json()["results"]

        assert {row["id"] for row in rows}.isdisjoint(str(m.id) for m in rival_team.values())

    def test_their_role_cannot_be_changed(self, team, rival_team):
        target = rival_team[Role.CLEANER]

        response = client_for(team[Role.OWNER]).patch(
            detail(target), {"role": Role.OWNER}, format="json"
        )

        target.refresh_from_db()
        assert response.status_code == 404
        assert target.role == Role.CLEANER

    @pytest.mark.parametrize("name", ["deactivate", "reactivate", "resend-invite"])
    def test_no_action_reaches_them(self, team, rival_team, name):
        target = rival_team[Role.CLEANER]

        response = client_for(team[Role.OWNER]).post(action(target, name))

        target.refresh_from_db()
        assert response.status_code == 404
        assert target.is_active is True

    def test_a_header_naming_their_organization_does_not_help(
        self, team, rival_team, other_organization
    ):
        response = client_for(team[Role.OWNER]).post(
            action(rival_team[Role.CLEANER], "deactivate"),
            headers={ORGANIZATION_HEADER.lower(): str(other_organization.id)},
        )

        rival_team[Role.CLEANER].refresh_from_db()
        assert response.status_code == 403
        assert rival_team[Role.CLEANER].is_active is True

    def test_an_owner_in_one_and_a_cleaner_in_another_is_a_cleaner_there(
        self, team, rival_team, other_organization
    ):
        """The role that counts is the one held in the organization acted in."""
        both = team[Role.OWNER].user
        Membership.objects.create(user=both, organization=other_organization, role=Role.CLEANER)
        client = client_for(both)
        headers = {ORGANIZATION_HEADER.lower(): str(other_organization.id)}

        deactivate = client.post(action(rival_team[Role.DISPATCHER], "deactivate"), headers=headers)
        invited = invite(client, headers=headers)

        assert deactivate.status_code == 403
        assert invited.status_code == 403
