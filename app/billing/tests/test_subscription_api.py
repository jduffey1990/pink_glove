"""
The subscription endpoints (Phase 4c): who may press them, what they
answer without Stripe, and that they stay open on a read-only
organization -- the one deliberate hole in the inactive rule, beside a
PATCH that is still refused.

`billing.subscription` is patched where it would call Stripe; the
permission line is what is under test.
"""

import datetime as dt
from unittest import mock

import pytest
from django.utils import timezone

from app.exceptions import ConflictError
from base.permissions import ORGANIZATION_INACTIVE
from billing import subscription
from users.enums import Role

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("platform_billing_on")]

CHECKOUT = "/api/billing/subscription/checkout/"
PORTAL = "/api/billing/subscription/portal/"
REFRESH = "/api/billing/subscription/refresh/"


def as_role(api_client, make_member, organization, role):
    api_client.force_login(make_member(organization, role))
    return api_client


@pytest.fixture
def deactivated(organization):
    organization.is_active = False
    organization.deactivated_for_billing_at = timezone.now()
    organization.trial_ends_at = timezone.now() - dt.timedelta(days=30)
    organization.save()
    return organization


class TestCheckout:
    def test_the_owner_is_sent_to_stripe(self, api_client, make_member, organization):
        client = as_role(api_client, make_member, organization, Role.OWNER)

        with mock.patch.object(
            subscription, "start_checkout", return_value="https://checkout.stripe.com/x"
        ) as start:
            response = client.post(CHECKOUT)

        assert response.status_code == 200
        assert response.data == {"url": "https://checkout.stripe.com/x"}
        assert start.call_args.args[0] == organization

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_everyone_else_is_refused(self, api_client, make_member, organization, role):
        client = as_role(api_client, make_member, organization, role)

        with mock.patch.object(subscription, "start_checkout") as start:
            assert client.post(CHECKOUT).status_code == 403
        start.assert_not_called()

    def test_anonymous_is_refused(self, api_client):
        assert api_client.post(CHECKOUT).status_code in (401, 403)

    def test_is_a_503_without_a_price(self, api_client, make_member, organization, settings):
        settings.PLATFORM_BILLING_ENABLED = False
        client = as_role(api_client, make_member, organization, Role.OWNER)

        response = client.post(CHECKOUT)

        assert response.status_code == 503
        assert response.data["detail"] == subscription.NOT_ENABLED

    def test_a_live_subscription_is_a_409(self, api_client, make_member, organization):
        client = as_role(api_client, make_member, organization, Role.OWNER)

        with mock.patch.object(
            subscription,
            "start_checkout",
            side_effect=ConflictError(subscription.ALREADY_SUBSCRIBED),
        ):
            response = client.post(CHECKOUT)

        assert response.status_code == 409
        assert response.data["detail"] == subscription.ALREADY_SUBSCRIBED


class TestPortal:
    def test_the_owner_opens_it(self, api_client, make_member, organization):
        client = as_role(api_client, make_member, organization, Role.OWNER)

        with mock.patch.object(
            subscription, "portal_url", return_value="https://billing.stripe.com/x"
        ):
            response = client.post(PORTAL)

        assert response.status_code == 200
        assert response.data == {"url": "https://billing.stripe.com/x"}

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_everyone_else_is_refused(self, api_client, make_member, organization, role):
        client = as_role(api_client, make_member, organization, role)

        assert client.post(PORTAL).status_code == 403

    def test_anonymous_is_refused(self, api_client):
        assert api_client.post(PORTAL).status_code in (401, 403)

    def test_nothing_to_manage_is_a_409(self, api_client, make_member, organization):
        client = as_role(api_client, make_member, organization, Role.OWNER)

        with mock.patch.object(subscription.connect, "client"):
            response = client.post(PORTAL)

        assert response.status_code == 409
        assert response.data["detail"] == subscription.NO_CUSTOMER


class TestRefresh:
    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
    def test_admin_and_above_get_the_fresh_block(self, api_client, make_member, organization, role):
        client = as_role(api_client, make_member, organization, role)

        with mock.patch.object(
            subscription, "refresh_subscription", side_effect=lambda o: o
        ) as refresh:
            response = client.post(REFRESH)

        assert response.status_code == 200
        assert response.data["state"] == "trial"
        assert response.data["available_actions"] == ["subscribe"]
        refresh.assert_called_once_with(organization)

    @pytest.mark.parametrize("role", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_below_admin_is_refused(self, api_client, make_member, organization, role):
        client = as_role(api_client, make_member, organization, role)

        assert client.post(REFRESH).status_code == 403

    def test_anonymous_is_refused(self, api_client):
        assert api_client.post(REFRESH).status_code in (401, 403)

    def test_the_notice_is_the_callers(self, api_client, make_member, organization):
        organization.trial_ends_at = timezone.now() - dt.timedelta(days=1)
        organization.save()
        client = as_role(api_client, make_member, organization, Role.ADMIN)

        with mock.patch.object(subscription, "refresh_subscription", side_effect=lambda o: o):
            response = client.post(REFRESH)

        assert "Ask the organization owner" in response.data["notice"]


class TestOnAReadOnlyOrganization:
    """The owner of a lapsed organization can still pay. Nothing else opens."""

    def test_checkout_still_answers(self, api_client, make_member, deactivated):
        client = as_role(api_client, make_member, deactivated, Role.OWNER)

        with mock.patch.object(subscription, "start_checkout", return_value="https://x"):
            assert client.post(CHECKOUT).status_code == 200

    def test_the_portal_still_answers(self, api_client, make_member, deactivated):
        client = as_role(api_client, make_member, deactivated, Role.OWNER)

        with mock.patch.object(subscription, "portal_url", return_value="https://x"):
            assert client.post(PORTAL).status_code == 200

    def test_refresh_still_answers(self, api_client, make_member, deactivated):
        client = as_role(api_client, make_member, deactivated, Role.ADMIN)

        with mock.patch.object(subscription, "refresh_subscription", side_effect=lambda o: o):
            response = client.post(REFRESH)

        assert response.status_code == 200
        assert response.data["state"] == "deactivated"

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_the_owner_line_still_holds_there(self, api_client, make_member, deactivated, role):
        """The hole is for the inactive rule only; everyone below the owner is refused as ever."""
        client = as_role(api_client, make_member, deactivated, role)

        with mock.patch.object(subscription, "start_checkout") as start:
            assert client.post(CHECKOUT).status_code == 403
            assert client.post(PORTAL).status_code == 403
        start.assert_not_called()

    @pytest.mark.parametrize("role", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_the_admin_line_still_holds_there(self, api_client, make_member, deactivated, role):
        client = as_role(api_client, make_member, deactivated, role)

        assert client.post(REFRESH).status_code == 403

    def test_and_the_organization_itself_is_still_read_only(
        self, api_client, make_member, deactivated
    ):
        client = as_role(api_client, make_member, deactivated, Role.OWNER)

        response = client.patch("/api/organizations/current/", {"name": "Nope"}, format="json")

        assert response.status_code == 403
        assert response.data["detail"] == ORGANIZATION_INACTIVE

    def test_the_connect_endpoints_are_not_exempt(self, api_client, make_member, deactivated):
        client = as_role(api_client, make_member, deactivated, Role.OWNER)

        assert client.post("/api/billing/stripe/connect/").status_code == 403
