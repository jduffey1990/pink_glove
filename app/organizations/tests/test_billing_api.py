"""
What the API says about the platform subscription (Phase 4c): the `billing`
block on the organization, and the summary on the session payload the
banner is drawn from.

The rule itself is tested in `test_billing_coverage.py`; this is the
publishing of it -- the shape, who it is written for, and that none of it
can be written back.
"""

import datetime as dt

import pytest
from django.utils import timezone

from app.middleware.tenant import ORGANIZATION_HEADER
from users.enums import Role

DAY = dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def billing_on(settings):
    settings.PLATFORM_BILLING_ENABLED = True
    settings.PLATFORM_GRACE_DAYS = 14


def as_role(api_client, make_member, organization, role):
    api_client.force_login(make_member(organization, role))
    return api_client


@pytest.mark.django_db
class TestTheBillingBlock:
    URL = "/api/organizations/current/"

    def test_a_trial_organization(self, authed_client, organization):
        billing = authed_client.get(self.URL).data["billing"]

        assert billing["state"] == "trial"
        assert billing["level"] is None
        assert billing["notice"] is None
        assert billing["trial_ends_at"] is not None
        assert billing["covered_until"] == billing["trial_ends_at"]
        assert billing["grace_ends_at"] is not None
        assert billing["status"] == ""
        assert billing["cancel_at_period_end"] is False
        assert billing["exempt"] is False
        assert billing["available_actions"] == ["subscribe"]
        assert billing["summary"].startswith("Your free trial runs until")

    def test_the_summary_is_the_notice_when_there_is_one(self, authed_client, organization):
        organization.trial_ends_at = timezone.now() - DAY
        organization.save()

        billing = authed_client.get(self.URL).data["billing"]

        assert billing["summary"] == billing["notice"]

    def test_the_tenants_customers_see_the_state_and_nothing_else(
        self, api_client, make_member, organization
    ):
        """How the cleaning company pays for its software is not its customers' business."""
        organization.trial_ends_at = timezone.now() - DAY
        organization.stripe_customer_id = "cus_x"
        organization.subscription_status = "canceled"
        organization.save()
        customer = as_role(api_client, make_member, organization, Role.CUSTOMER)

        billing = customer.get(self.URL).data["billing"]
        current = customer.get("/api/users/session/").data["current_organization"]

        assert billing == {
            "state": "lapsed",
            "level": None,
            "notice": None,
            "summary": "",
            "trial_ends_at": None,
            "covered_until": None,
            "grace_ends_at": None,
            "status": "",
            "cancel_at_period_end": False,
            "exempt": False,
            "available_actions": [],
        }
        assert current["billing"] == {"state": "lapsed", "level": None, "notice": None}

    def test_the_grace_follows_the_coverage(self, authed_client, organization):
        billing = authed_client.get(self.URL).data["billing"]

        covered = dt.datetime.fromisoformat(billing["covered_until"])
        grace = dt.datetime.fromisoformat(billing["grace_ends_at"])
        assert grace - covered == 14 * DAY

    def test_a_complimentary_organization(self, authed_client, organization):
        organization.billing_exempt = True
        organization.save()

        billing = authed_client.get(self.URL).data["billing"]

        assert billing["state"] == "exempt"
        assert billing["exempt"] is True
        assert billing["covered_until"] is None
        assert billing["available_actions"] == []

    def test_a_subscriber(self, authed_client, organization):
        organization.trial_ends_at = timezone.now() - 40 * DAY
        organization.stripe_customer_id = "cus_x"
        organization.stripe_subscription_id = "sub_x"
        organization.subscription_status = "active"
        organization.subscription_paid_through = timezone.now() + 20 * DAY
        organization.subscription_cancel_at_period_end = True
        organization.save()

        billing = authed_client.get(self.URL).data["billing"]

        assert billing["state"] == "active"
        assert billing["status"] == "active"
        assert billing["cancel_at_period_end"] is True
        assert billing["level"] == "info"
        assert billing["notice"].startswith("Your subscription ends on")
        assert billing["available_actions"] == ["manage"]

    def test_the_notice_is_written_for_the_reader(self, api_client, make_member, organization):
        organization.trial_ends_at = timezone.now() - DAY
        organization.save()

        owner = as_role(api_client, make_member, organization, Role.OWNER)
        assert "Subscribe by" in owner.get(self.URL).data["billing"]["notice"]
        owner.logout()

        dispatcher = as_role(api_client, make_member, organization, Role.DISPATCHER)
        assert "Ask the organization owner" in dispatcher.get(self.URL).data["billing"]["notice"]

    def test_a_superuser_reads_as_the_owner(self, api_client, superuser, organization):
        organization.trial_ends_at = timezone.now() - DAY
        organization.save()
        api_client.force_login(superuser)

        response = api_client.get(self.URL, headers={ORGANIZATION_HEADER: str(organization.id)})

        assert "Subscribe by" in response.data["billing"]["notice"]

    def test_nothing_in_the_block_can_be_written(self, authed_client, organization):
        """A forged PATCH is ignored field by field, not refused: these are not inputs."""
        response = authed_client.patch(
            self.URL,
            {
                "billing_exempt": True,
                "trial_ends_at": "2099-01-01T00:00:00Z",
                "subscription_status": "active",
                "stripe_customer_id": "cus_forged",
                "billing": {"state": "exempt", "exempt": True},
            },
            format="json",
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.billing_exempt is False
        assert organization.trial_ends_at.year != 2099
        assert organization.subscription_status == ""
        assert organization.stripe_customer_id == ""
        assert response.data["billing"]["state"] == "trial"

    def test_is_exempt_when_billing_is_not_configured(self, authed_client, organization, settings):
        """`exempt` with `exempt: false`, which the card treats as nothing to show."""
        settings.PLATFORM_BILLING_ENABLED = False
        organization.trial_ends_at = timezone.now() - 100 * DAY
        organization.save()

        billing = authed_client.get(self.URL).data["billing"]

        assert billing["state"] == "exempt"
        assert billing["notice"] is None
        assert billing["available_actions"] == []


@pytest.mark.django_db
class TestTheSessionPayload:
    URL = "/api/users/session/"

    def test_the_current_organization_carries_is_active_and_the_notice(
        self, authed_client, organization
    ):
        organization.trial_ends_at = timezone.now() - DAY
        organization.save()

        current = authed_client.get(self.URL).data["current_organization"]

        assert current["is_active"] is True
        assert current["billing"] == {
            "state": "lapsed",
            "level": "warning",
            "notice": current["billing"]["notice"],
        }
        assert "Subscribe by" in current["billing"]["notice"]

    def test_a_deactivated_organization_says_so_on_the_session(self, authed_client, organization):
        organization.is_active = False
        organization.save()

        current = authed_client.get(self.URL).data["current_organization"]

        assert current["is_active"] is False
        assert current["billing"]["state"] == "deactivated"
        assert current["billing"]["level"] == "error"

    def test_each_membership_is_written_in_its_own_role(
        self, api_client, make_member, organization, other_organization
    ):
        """One person, owner here and cleaner there: two different sentences."""
        for org in (organization, other_organization):
            org.trial_ends_at = timezone.now() - DAY
            org.save()
        user = make_member(organization, Role.OWNER, email="two@example.com")
        from users.models import Membership

        Membership.objects.create(user=user, organization=other_organization, role=Role.CLEANER)
        api_client.force_login(user)

        memberships = api_client.get(
            self.URL, headers={ORGANIZATION_HEADER: str(organization.id)}
        ).data["memberships"]

        by_org = {
            m["organization"]["id"]: m["organization"]["billing"]["notice"] for m in memberships
        }
        assert "Subscribe by" in by_org[str(organization.id)]
        assert "Ask the organization owner" in by_org[str(other_organization.id)]

    def test_the_staff_directory_is_written_for_the_caller(
        self, api_client, make_member, organization
    ):
        """Every row is the same organization; the sentence is the reader's, not the row's."""
        organization.trial_ends_at = timezone.now() - DAY
        organization.save()
        make_member(organization, Role.OWNER, email="boss@example.com")
        dispatcher = as_role(api_client, make_member, organization, Role.DISPATCHER)

        rows = dispatcher.get("/api/users/memberships/").data["results"]

        assert len(rows) >= 2
        assert all(
            "Ask the organization owner" in r["organization"]["billing"]["notice"] for r in rows
        )
