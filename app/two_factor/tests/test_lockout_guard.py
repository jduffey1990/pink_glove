"""
Team management must not change how anyone already here signs in.

Every membership that existed before invitations did was made by the seed or
in the Django admin, with `invited_at` and `accepted_at` both NULL. Nothing
may gate sign-in on either column, or the day this ships is the day every
existing owner is locked out (ADR-034). These walk the three sign-in paths
of ADR-008 for exactly such a person.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from django.utils import timezone

from two_factor.models import TrustedDevice
from two_factor.services import TRUSTED_DEVICE_COOKIE
from users.enums import MembershipState, Role
from users.models import CustomUser, Membership

pytestmark = pytest.mark.django_db

LOGIN = reverse("two_factor:login")
VERIFY = reverse("two_factor:verify")
SESSION = reverse("users:session")
DIRECTORY = reverse("users:membership-list")
PASSWORD = "pw-for-tests-only"  # noqa: S105


@pytest.fixture(autouse=True)
def _local(settings):
    settings.LOCAL = True


def _login(client, user):
    return client.post(LOGIN, {"email": user.email, "password": PASSWORD}, format="json")


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.DISPATCHER])
def test_office_staff_made_before_invitations_sign_in_as_before(
    api_client, organization, make_member, role
):
    user = make_member(organization, role=role)
    membership = user.memberships.get()
    assert (membership.invited_at, membership.accepted_at) == (None, None)

    login = _login(api_client, user)
    assert login.status_code == 202
    verify = api_client.post(VERIFY, {"code": login.json()["dev_code"]}, format="json")
    assert verify.status_code == 200

    session = api_client.get(SESSION).json()
    assert session["current_role"] == role
    assert session["current_organization"]["id"] == str(organization.id)
    assert [row["id"] for row in session["memberships"]] == [str(membership.id)]
    assert api_client.get(DIRECTORY).status_code == 200


def test_a_cleaner_made_before_invitations_still_skips_the_code_on_a_trusted_device(
    api_client, organization, make_member
):
    cleaner = make_member(organization, role=Role.CLEANER)
    _, device = TrustedDevice.issue(cleaner)
    api_client.cookies[TRUSTED_DEVICE_COOKIE] = device

    login = _login(api_client, cleaner)

    assert login.status_code == 200
    assert api_client.get(SESSION).json()["current_role"] == Role.CLEANER
    assert api_client.get(DIRECTORY).status_code == 200


def test_a_customer_made_before_invitations_still_signs_in_by_magic_link(api_client, organization):
    customer = CustomUser.objects.create_user(email="homeowner@example.com")
    Membership.objects.create(user=customer, organization=organization, role=Role.CUSTOMER)

    link = api_client.post(reverse("users:magic-link-request"), {"email": customer.email}).json()
    # The token is in the link's fragment (ADR-036, ADR-039).
    token = parse_qs(urlsplit(link["dev_link"]).fragment)["token"][0]
    response = api_client.post(reverse("users:magic-link-consume"), {"token": token})

    assert response.status_code == 200
    assert api_client.get(SESSION).json()["current_role"] == Role.CUSTOMER


def test_such_a_membership_reads_as_active_on_the_team_page(authed_client, owner):
    membership = owner.memberships.get()

    row = authed_client.get(reverse("users:membership-detail", args=[membership.id])).json()

    assert membership.state == MembershipState.ACTIVE
    assert row["state"] == "active"


def test_an_invite_date_typed_into_the_admin_does_not_switch_anyone_off(
    api_client, organization, make_member
):
    """
    The admin inline shows `invited_at`. Someone filling it in on an active
    membership has not sent an invitation, and must not have locked the
    person out: access follows `is_active` and nothing else.
    """
    user = make_member(organization, role=Role.OWNER)
    Membership.objects.filter(user=user).update(invited_at=timezone.now())

    login = _login(api_client, user)
    api_client.post(VERIFY, {"code": login.json()["dev_code"]}, format="json")

    assert api_client.get(SESSION).json()["current_role"] == Role.OWNER
    assert api_client.get(DIRECTORY).status_code == 200
