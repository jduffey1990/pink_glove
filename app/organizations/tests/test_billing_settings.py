"""
The settings the invoice rules read: the tax rate, the no-access fee, and the
invoice numbering and terms.

`no_access_fee_cents` is the only one with arithmetic in it, so it carries most
of the weight here. All three fee types are exercised, because NONE returning 0
and FLAT returning 0 mean different things to the caller: the first skips the
line, the second is a rate somebody set to zero.
"""

from decimal import Decimal

import pytest

from organizations.enums import NoAccessFeeType
from organizations.models import Organization
from users.enums import Role


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Fee Test Clean")


@pytest.mark.django_db
class TestNoAccessFeeCents:
    def test_none_is_zero_whatever_the_value_says(self, org):
        # A leftover value from a fee that was switched off must not leak back.
        org.no_access_fee_type = NoAccessFeeType.NONE
        org.no_access_fee_value = Decimal("2500")

        assert org.no_access_fee_cents(20000) == 0

    def test_flat_ignores_the_job_price(self, org):
        org.no_access_fee_type = NoAccessFeeType.FLAT
        org.no_access_fee_value = Decimal("2500")

        assert org.no_access_fee_cents(20000) == 2500
        assert org.no_access_fee_cents(0) == 2500

    def test_percent_is_taken_off_the_job_price(self, org):
        org.no_access_fee_type = NoAccessFeeType.PERCENT
        org.no_access_fee_value = Decimal("50")

        assert org.no_access_fee_cents(20000) == 10000

    def test_a_half_cent_rounds_up(self, org):
        # 33.333% of $1.50 is 49.9995 cents; half up gives 50, not 49.
        org.no_access_fee_type = NoAccessFeeType.PERCENT
        org.no_access_fee_value = Decimal("33.333")

        assert org.no_access_fee_cents(150) == 50

    def test_exactly_half_a_cent_rounds_up_not_to_even(self, org):
        # 50% of 1 cent is 0.5; ROUND_HALF_UP gives 1, banker's rounding 0.
        org.no_access_fee_type = NoAccessFeeType.PERCENT
        org.no_access_fee_value = Decimal("50")

        assert org.no_access_fee_cents(1) == 1

    def test_the_result_is_whole_cents(self, org):
        org.no_access_fee_type = NoAccessFeeType.PERCENT
        org.no_access_fee_value = Decimal("12.5")

        fee = org.no_access_fee_cents(12345)
        assert isinstance(fee, int)

    def test_the_default_organization_charges_nothing(self, org):
        assert org.no_access_fee_type == NoAccessFeeType.NONE
        assert org.no_access_fee_cents(20000) == 0


@pytest.mark.django_db
class TestDefaults:
    """Every setting is defaulted, so no existing tenant needs a backfill."""

    def test_are_a_working_invoice_configuration(self, org):
        assert org.tax_rate_percent == Decimal("0.000")
        assert org.invoice_prefix == "INV"
        assert org.invoice_terms_days == 14
        assert org.invoice_footer == ""


@pytest.mark.django_db
class TestApi:
    """The settings ride the existing organization endpoint and its gating."""

    def test_a_member_can_read_them(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.CLEANER))

        response = api_client.get("/api/organizations/current/")

        assert response.status_code == 200
        assert response.data["tax_rate_percent"] == "0.000"
        assert response.data["no_access_fee_type"] == NoAccessFeeType.NONE
        assert response.data["invoice_prefix"] == "INV"

    def test_an_admin_can_change_them(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.ADMIN))

        response = api_client.patch(
            "/api/organizations/current/",
            {
                "tax_rate_percent": "8.250",
                "no_access_fee_type": NoAccessFeeType.FLAT,
                "no_access_fee_value": "2500.000",
                "invoice_prefix": "SPK",
                "invoice_terms_days": 30,
                "invoice_footer": "Make checks payable to Sparkle Clean.",
            },
            format="json",
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.tax_rate_percent == Decimal("8.250")
        assert organization.no_access_fee_cents(20000) == 2500
        assert organization.invoice_terms_days == 30

    def test_the_stripe_block_is_published(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.DISPATCHER))

        response = api_client.get("/api/organizations/current/")

        assert response.data["stripe"] == {
            "connected": False,
            "charges_enabled": False,
            "details_submitted": False,
            "connected_at": None,
        }

    def test_the_stripe_block_cannot_be_written(self, api_client, organization, make_member):
        """
        Only `billing.connect` and the webhook write these (4b.1). An owner
        claiming an account id through the settings form would point another
        tenant's payments at this one.
        """
        api_client.force_login(make_member(organization, Role.OWNER))

        response = api_client.patch(
            "/api/organizations/current/",
            {
                "stripe_account_id": "acct_forged",
                "stripe_charges_enabled": True,
                "stripe": {"connected": True, "charges_enabled": True},
            },
            format="json",
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.stripe_account_id == ""
        assert organization.stripe_charges_enabled is False
        assert response.data["stripe"]["connected"] is False

    def test_an_owner_can_too(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.OWNER))

        response = api_client.patch(
            "/api/organizations/current/", {"invoice_terms_days": 7}, format="json"
        )

        assert response.status_code == 200
        organization.refresh_from_db()
        assert organization.invoice_terms_days == 7

    @pytest.mark.parametrize("role", [Role.CLEANER, Role.CUSTOMER])
    def test_neither_a_cleaner_nor_a_customer_can(
        self, api_client, organization, make_member, role
    ):
        api_client.force_login(make_member(organization, role))

        response = api_client.patch(
            "/api/organizations/current/", {"invoice_prefix": "HAX"}, format="json"
        )

        assert response.status_code == 403
        organization.refresh_from_db()
        assert organization.invoice_prefix == "INV"

    def test_a_negative_no_access_fee_is_refused(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.ADMIN))

        response = api_client.patch(
            "/api/organizations/current/", {"no_access_fee_value": "-1.000"}, format="json"
        )

        assert response.status_code == 400
        assert "no_access_fee_value" in response.data

    def test_an_over_long_prefix_is_refused(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.ADMIN))

        response = api_client.patch(
            "/api/organizations/current/", {"invoice_prefix": "TOOLONGPREFIX"}, format="json"
        )

        assert response.status_code == 400
        assert "invoice_prefix" in response.data

    def test_a_dispatcher_cannot(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.DISPATCHER))

        response = api_client.patch(
            "/api/organizations/current/", {"tax_rate_percent": "8.250"}, format="json"
        )

        assert response.status_code == 403
        organization.refresh_from_db()
        assert organization.tax_rate_percent == Decimal("0.000")

    def test_a_negative_tax_rate_is_refused(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.ADMIN))

        response = api_client.patch(
            "/api/organizations/current/", {"tax_rate_percent": "-1.000"}, format="json"
        )

        assert response.status_code == 400
        assert "tax_rate_percent" in response.data

    def test_an_unknown_fee_type_is_refused(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, Role.ADMIN))

        response = api_client.patch(
            "/api/organizations/current/", {"no_access_fee_type": "sometimes"}, format="json"
        )

        assert response.status_code == 400
        assert "no_access_fee_type" in response.data
