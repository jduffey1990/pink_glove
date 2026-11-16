"""
`AccountToken`: single-use, hashed, expiring, and good for one purpose only.

`Membership.state` is here too, since the token and the state are the two
things the model layer adds for team management (ADR-028).
"""

import hashlib
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from users.enums import AccountTokenPurpose, MembershipState, Role
from users.models import AccountToken, MagicLinkToken, Membership

pytestmark = pytest.mark.django_db

INVITE = AccountTokenPurpose.INVITE
RESET = AccountTokenPurpose.PASSWORD_RESET


@pytest.fixture
def membership(owner):
    return owner.memberships.get()


class TestIssue:
    def test_the_raw_token_is_never_stored(self, owner):
        token, raw = AccountToken.issue(RESET, owner)

        assert token.token_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert raw != token.token_hash
        assert len(raw) >= 43  # 32 random bytes, url-safe

    def test_each_purpose_has_its_own_lifetime(self, owner, membership):
        invite, _ = AccountToken.issue(INVITE, owner, membership)
        reset, _ = AccountToken.issue(RESET, owner)

        assert timedelta(days=6, hours=23) < invite.expires_at - timezone.now() <= timedelta(days=7)
        assert timedelta(minutes=59) < reset.expires_at - timezone.now() <= timedelta(hours=1)

    def test_a_new_one_withdraws_the_earlier_one(self, owner):
        _, first = AccountToken.issue(RESET, owner)
        _, second = AccountToken.issue(RESET, owner)

        assert AccountToken.find(RESET, first) is None
        assert AccountToken.find(RESET, second) is not None

    def test_a_reset_does_not_withdraw_an_invitation(self, owner, membership):
        _, invite = AccountToken.issue(INVITE, owner, membership)
        AccountToken.issue(RESET, owner)

        assert AccountToken.find(INVITE, invite) is not None

    def test_one_invitation_does_not_withdraw_another_organizations(
        self, owner, membership, other_organization
    ):
        elsewhere = Membership.objects.create(
            user=owner, organization=other_organization, role=Role.CLEANER, is_active=False
        )
        _, here = AccountToken.issue(INVITE, owner, membership)
        _, there = AccountToken.issue(INVITE, owner, elsewhere)

        assert AccountToken.find(INVITE, here) is not None
        assert AccountToken.find(INVITE, there) is not None

    def test_an_invitation_must_name_a_membership(self, owner):
        with pytest.raises(IntegrityError), transaction.atomic():
            AccountToken.issue(INVITE, owner)

    def test_a_reset_must_not_name_one(self, owner, membership):
        with pytest.raises(IntegrityError), transaction.atomic():
            AccountToken.issue(RESET, owner, membership)


class TestFindAndConsume:
    def test_it_is_only_found_under_its_own_purpose(self, owner, membership):
        _, invite = AccountToken.issue(INVITE, owner, membership)
        _, reset = AccountToken.issue(RESET, owner)

        assert AccountToken.find(RESET, invite) is None
        assert AccountToken.find(INVITE, reset) is None

    def test_it_is_not_a_magic_link(self, owner, membership):
        _, invite = AccountToken.issue(INVITE, owner, membership)
        _, reset = AccountToken.issue(RESET, owner)

        assert MagicLinkToken.consume(invite) is None
        assert MagicLinkToken.consume(reset) is None

    def test_a_magic_link_is_not_one_of_these(self, owner):
        _, magic = MagicLinkToken.issue(owner)

        assert AccountToken.find(INVITE, magic) is None
        assert AccountToken.find(RESET, magic) is None

    def test_finding_spends_nothing(self, owner):
        _, raw = AccountToken.issue(RESET, owner)

        assert AccountToken.find(RESET, raw) is not None
        assert AccountToken.find(RESET, raw) is not None

    def test_it_is_consumed_once(self, owner):
        _, raw = AccountToken.issue(RESET, owner)
        token = AccountToken.find(RESET, raw)

        assert token.consume() is True
        assert token.consume() is False
        assert AccountToken.find(RESET, raw) is None

    def test_two_copies_of_one_token_cannot_both_be_consumed(self, owner):
        """The race the conditional update is for: both passed `find` first."""
        _, raw = AccountToken.issue(RESET, owner)
        first = AccountToken.find(RESET, raw)
        second = AccountToken.find(RESET, raw)

        assert first.consume() is True
        assert second.consume() is False

    def test_an_expired_one_is_not_found_and_cannot_be_consumed(self, owner):
        token, raw = AccountToken.issue(RESET, owner)
        stale = AccountToken.objects.get(pk=token.pk)
        AccountToken.objects.filter(pk=token.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        assert AccountToken.find(RESET, raw) is None
        # A copy loaded while it was still live does not get to use it either.
        assert stale.consume() is False

    def test_an_unknown_token_is_not_found(self):
        assert AccountToken.find(RESET, "not-a-token") is None
        assert AccountToken.find(INVITE, "") is None

    def test_withdrawing_spends_every_live_one(self, owner):
        _, raw = AccountToken.issue(RESET, owner)

        assert AccountToken.withdraw(RESET, user=owner) == 1
        assert AccountToken.find(RESET, raw) is None


class TestMembershipState:
    def test_a_seeded_membership_is_active(self, membership):
        """Both timestamps NULL, as every membership made before this was."""
        assert membership.invited_at is None
        assert membership.accepted_at is None
        assert membership.state == MembershipState.ACTIVE

    def test_an_invitation_is_invited(self, membership):
        membership.is_active = False
        membership.invited_at = timezone.now()

        assert membership.state == MembershipState.INVITED

    def test_an_accepted_invitation_is_active(self, membership):
        membership.invited_at = membership.accepted_at = timezone.now()

        assert membership.state == MembershipState.ACTIVE

    def test_a_withdrawn_invitation_is_deactivated(self, membership):
        membership.is_active = False
        membership.invited_at = membership.deactivated_at = timezone.now()

        assert membership.state == MembershipState.DEACTIVATED

    def test_switched_off_in_the_admin_is_deactivated(self, membership):
        membership.is_active = False

        assert membership.state == MembershipState.DEACTIVATED

    def test_someone_who_accepted_and_was_switched_off_is_deactivated(self, membership):
        membership.is_active = False
        membership.invited_at = membership.accepted_at = timezone.now()

        assert membership.state == MembershipState.DEACTIVATED
