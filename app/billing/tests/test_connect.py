"""
`billing.connect`, with Stripe patched out at the client.

What is tested is what this module decides: when an account is created and
when not, what a session is asked for, what the pay token carries, and why
an invoice is or is not payable. The SDK's own behaviour is not.
"""

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core import signing

from app.exceptions import ConflictError, ServiceUnavailableError
from billing import connect
from billing.enums import InvoiceStatus
from billing.tests.factories import InvoiceFactory, IssuedInvoiceFactory, PaymentFactory
from scheduling.tests.factories import OrganizationFactory

pytestmark = pytest.mark.django_db

ACCOUNT = "acct_1TestConnectedAcct"


@pytest.fixture(autouse=True)
def stripe_on(settings):
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    settings.STRIPE_APPLICATION_FEE_PERCENT = Decimal("0")
    settings.FRONTEND_BASE_URL = "https://app.example.test"


@pytest.fixture
def api():
    """A fake StripeClient whose calls the tests inspect."""
    fake = mock.MagicMock(name="StripeClient")
    fake.v1.accounts.create.return_value = SimpleNamespace(id=ACCOUNT)
    fake.v1.accounts.retrieve.return_value = SimpleNamespace(
        id=ACCOUNT, charges_enabled=False, details_submitted=False
    )
    fake.v1.account_links.create.return_value = SimpleNamespace(
        url="https://connect.stripe.com/setup/x"
    )
    fake.v1.checkout.sessions.create.return_value = SimpleNamespace(
        url="https://checkout.stripe.com/c/pay/x"
    )
    with mock.patch.object(connect, "client", return_value=fake):
        yield fake


@pytest.fixture
def connected():
    return OrganizationFactory(stripe_account_id=ACCOUNT, stripe_charges_enabled=True)


class TestDisabled:
    def test_every_entry_point_is_a_503_without_a_key(self, settings):
        settings.STRIPE_ENABLED = False
        organization = OrganizationFactory()

        with pytest.raises(ServiceUnavailableError):
            connect.start_onboarding(organization)
        with pytest.raises(ServiceUnavailableError):
            connect.create_checkout_session(IssuedInvoiceFactory(organization=organization))

    def test_nothing_is_payable_without_a_key(self, settings, connected):
        settings.STRIPE_ENABLED = False

        assert connect.payable(IssuedInvoiceFactory(organization=connected)) == connect.NOT_ENABLED


class TestStartOnboarding:
    def test_creates_a_standard_account_once_and_a_link_every_time(self, api):
        organization = OrganizationFactory(email="owner@sparkle.test")

        first = connect.start_onboarding(organization)
        second = connect.start_onboarding(organization)

        assert first == second == "https://connect.stripe.com/setup/x"
        assert api.v1.accounts.create.call_count == 1
        assert api.v1.account_links.create.call_count == 2
        organization.refresh_from_db()
        assert organization.stripe_account_id == ACCOUNT

    def test_the_account_is_a_standard_one_on_the_tenants_own_terms(self, api):
        connect.start_onboarding(OrganizationFactory(name="Sparkle Clean", email="o@s.test"))

        params = api.v1.accounts.create.call_args.args[0]
        assert params["controller"]["stripe_dashboard"] == {"type": "full"}
        assert params["controller"]["fees"] == {"payer": "account"}
        assert params["controller"]["losses"] == {"payments": "stripe"}
        assert params["business_profile"] == {"name": "Sparkle Clean"}
        assert params["email"] == "o@s.test"

    def test_the_link_returns_to_the_settings_page(self, api):
        connect.start_onboarding(OrganizationFactory())

        params = api.v1.account_links.create.call_args.args[0]
        assert params["account"] == ACCOUNT
        assert params["type"] == "account_onboarding"
        assert params["return_url"] == "https://app.example.test/billing/settings?stripe=return"
        assert params["refresh_url"] == "https://app.example.test/billing/settings?stripe=refresh"

    def test_an_account_already_taking_payments_is_not_re_onboarded(self, api, connected):
        with pytest.raises(ConflictError):
            connect.start_onboarding(connected)

        api.v1.accounts.create.assert_not_called()
        api.v1.account_links.create.assert_not_called()


class TestRefreshAccount:
    def test_copies_the_flags_and_stamps_connected_at_once(self, api):
        organization = OrganizationFactory(stripe_account_id=ACCOUNT)
        api.v1.accounts.retrieve.return_value = SimpleNamespace(
            charges_enabled=True, details_submitted=True
        )

        connect.refresh_account(organization)
        organization.refresh_from_db()
        first_stamp = organization.stripe_connected_at

        assert organization.stripe_charges_enabled is True
        assert organization.stripe_details_submitted is True
        assert first_stamp is not None

        connect.refresh_account(organization)
        organization.refresh_from_db()
        assert organization.stripe_connected_at == first_stamp

    def test_charges_can_go_back_off_but_the_stamp_stays(self, api, connected):
        api.v1.accounts.retrieve.return_value = SimpleNamespace(
            charges_enabled=True, details_submitted=True
        )
        connect.refresh_account(connected)  # stamps
        api.v1.accounts.retrieve.return_value = SimpleNamespace(
            charges_enabled=False, details_submitted=True
        )

        connect.refresh_account(connected)
        connected.refresh_from_db()

        assert connected.stripe_charges_enabled is False
        assert connected.stripe_connected_at is not None

    def test_an_organization_with_no_account_is_left_alone(self, api):
        organization = OrganizationFactory()

        connect.refresh_account(organization)

        api.v1.accounts.retrieve.assert_not_called()


class TestPayToken:
    def test_round_trips_to_the_invoice(self):
        invoice = IssuedInvoiceFactory()

        assert connect.invoice_from_pay_token(connect.pay_token(invoice)) == invoice

    def test_a_tampered_token_finds_nothing(self):
        token = connect.pay_token(IssuedInvoiceFactory())

        assert connect.invoice_from_pay_token(token[:-3] + "xyz") is None
        assert connect.invoice_from_pay_token("") is None

    def test_an_expired_token_finds_nothing(self):
        invoice = IssuedInvoiceFactory()
        token = connect.pay_token(invoice)
        later = dt.datetime.now(dt.UTC) + connect.PAY_TOKEN_MAX_AGE + dt.timedelta(days=1)

        with mock.patch("django.core.signing.time.time", return_value=later.timestamp()):
            assert connect.invoice_from_pay_token(token) is None

    def test_a_token_from_another_purpose_is_refused(self):
        invoice = IssuedInvoiceFactory()
        token = signing.dumps(str(invoice.pk), salt="something-else")

        assert connect.invoice_from_pay_token(token) is None

    def test_the_url_is_under_the_frontend(self):
        invoice = IssuedInvoiceFactory()

        assert connect.pay_url(invoice).startswith("https://app.example.test/pay/")


class TestPayable:
    def test_an_open_invoice_on_a_connected_account_is_payable(self, connected):
        assert connect.payable(IssuedInvoiceFactory(organization=connected)) is None

    def test_an_organization_not_taking_cards(self):
        organization = OrganizationFactory(name="Rival Cleaners", stripe_account_id=ACCOUNT)

        reason = connect.payable(IssuedInvoiceFactory(organization=organization))

        assert reason == "Rival Cleaners does not take card payments online."

    def test_a_draft_and_a_void_are_not_open(self, connected):
        assert "not open" in connect.payable(InvoiceFactory(organization=connected))
        void = IssuedInvoiceFactory(organization=connected, status=InvoiceStatus.VOID)
        assert "not open" in connect.payable(void)

    def test_a_settled_invoice_is_paid(self, connected):
        invoice = IssuedInvoiceFactory(organization=connected, total_cents=15000)
        PaymentFactory(organization=connected, invoice=invoice, amount_cents=15000)

        assert connect.payable(invoice) == "This invoice is paid."


class TestCheckoutSession:
    def test_charges_the_balance_on_the_connected_account(self, api, connected):
        invoice = IssuedInvoiceFactory(
            organization=connected, total_cents=20000, bill_to_email="c@x.test", number="SPK-0007"
        )
        PaymentFactory(organization=connected, invoice=invoice, amount_cents=5000)

        url = connect.create_checkout_session(invoice)

        assert url == "https://checkout.stripe.com/c/pay/x"
        (params,) = api.v1.checkout.sessions.create.call_args.args
        options = api.v1.checkout.sessions.create.call_args.kwargs["options"]
        assert options == {"stripe_account": ACCOUNT}
        assert params["mode"] == "payment"
        line = params["line_items"][0]["price_data"]
        assert line["unit_amount"] == 15000
        assert line["currency"] == "usd"
        assert line["product_data"]["name"] == f"Invoice SPK-0007 from {connected.name}"
        assert params["customer_email"] == "c@x.test"
        assert params["client_reference_id"] == str(invoice.pk)

    def test_the_invoice_rides_on_the_session_and_on_the_intent(self, api, connected):
        invoice = IssuedInvoiceFactory(organization=connected)

        connect.create_checkout_session(invoice)

        params = api.v1.checkout.sessions.create.call_args.args[0]
        expected = {"organization_id": str(connected.pk), "invoice_id": str(invoice.pk)}
        assert params["metadata"] == expected
        assert params["payment_intent_data"]["metadata"] == expected

    def test_returns_to_the_pay_page_either_way(self, api, connected):
        invoice = IssuedInvoiceFactory(organization=connected)

        connect.create_checkout_session(invoice)

        params = api.v1.checkout.sessions.create.call_args.args[0]
        assert params["success_url"] == connect.pay_url(invoice) + "?paid=1"
        assert params["cancel_url"] == connect.pay_url(invoice) + "?cancelled=1"

    def test_expires_in_thirty_minutes(self, api, connected):
        before = dt.datetime.now(dt.UTC)
        connect.create_checkout_session(IssuedInvoiceFactory(organization=connected))

        expires = api.v1.checkout.sessions.create.call_args.args[0]["expires_at"]
        assert 29 * 60 <= expires - before.timestamp() <= 31 * 60

    def test_no_fee_at_the_shipped_setting(self, api, connected):
        connect.create_checkout_session(IssuedInvoiceFactory(organization=connected))

        intent = api.v1.checkout.sessions.create.call_args.args[0]["payment_intent_data"]
        assert "application_fee_amount" not in intent

    def test_a_fee_is_a_percentage_rounded_once_half_up(self, api, connected, settings):
        settings.STRIPE_APPLICATION_FEE_PERCENT = Decimal("2.9")
        invoice = IssuedInvoiceFactory(organization=connected, total_cents=12345)

        connect.create_checkout_session(invoice)

        intent = api.v1.checkout.sessions.create.call_args.args[0]["payment_intent_data"]
        assert intent["application_fee_amount"] == 358  # 357.999 -> 358

    def test_refuses_what_is_not_payable(self, api, connected):
        with pytest.raises(ConflictError):
            connect.create_checkout_session(InvoiceFactory(organization=connected))

        api.v1.checkout.sessions.create.assert_not_called()


class TestFee:
    def test_reads_the_balance_transaction_on_the_connected_account(self, api, connected):
        api.v1.payment_intents.retrieve.return_value = SimpleNamespace(
            latest_charge=SimpleNamespace(balance_transaction=SimpleNamespace(fee=317))
        )

        assert connect.fee_cents_for("pi_1", organization=connected) == 317
        options = api.v1.payment_intents.retrieve.call_args.kwargs["options"]
        assert options == {"stripe_account": ACCOUNT}

    def test_is_none_until_stripe_has_settled_it(self, api, connected):
        api.v1.payment_intents.retrieve.return_value = SimpleNamespace(latest_charge=None)

        assert connect.fee_cents_for("pi_1", organization=connected) is None

    def test_a_failed_read_is_none_not_an_error(self, api, connected):
        api.v1.payment_intents.retrieve.side_effect = RuntimeError("stripe is down")

        assert connect.fee_cents_for("pi_1", organization=connected) is None
