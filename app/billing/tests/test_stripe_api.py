"""
The Connect endpoints, the public pay page, and what the invoice and
payment serializers say about Stripe.

`billing.connect` is patched where it would call Stripe; the permission
line, the token, and the server-owned fields are what is under test.
"""

from unittest import mock

import pytest
from django.core import mail

from app.exceptions import ConflictError
from billing import connect, services
from billing.tests.conftest import completed_job
from billing.tests.factories import IssuedInvoiceFactory, PaymentFactory
from billing.tests.stripe_fixtures import ACCOUNT_ID
from scheduling.tests.factories import OrganizationFactory
from users.enums import Role

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("stripe_on")]


def as_role(api_client, make_member, organization, role):
    api_client.force_login(make_member(organization, role))
    return api_client


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


class TestConnect:
    URL = "/api/billing/stripe/connect/"

    def test_the_owner_is_sent_to_stripe(self, api_client, make_member, organization):
        client = as_role(api_client, make_member, organization, Role.OWNER)

        with mock.patch.object(
            connect, "start_onboarding", return_value="https://connect.stripe.com/x"
        ) as start:
            response = client.post(self.URL)

        assert response.status_code == 200
        assert response.data == {"url": "https://connect.stripe.com/x"}
        start.assert_called_once()
        assert start.call_args.args[0] == organization

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_everyone_else_is_refused(self, api_client, make_member, organization, role):
        client = as_role(api_client, make_member, organization, role)

        with mock.patch.object(connect, "start_onboarding") as start:
            assert client.post(self.URL).status_code == 403
        start.assert_not_called()

    def test_anonymous_is_refused(self, api_client):
        assert api_client.post(self.URL).status_code in (401, 403)

    def test_is_a_503_without_stripe(self, api_client, make_member, organization, settings):
        settings.STRIPE_ENABLED = False
        client = as_role(api_client, make_member, organization, Role.OWNER)

        response = client.post(self.URL)

        assert response.status_code == 503
        assert response.data["detail"] == connect.NOT_ENABLED

    def test_already_taking_payments_is_a_409(self, api_client, make_member, connected):
        client = as_role(api_client, make_member, connected, Role.OWNER)

        with mock.patch.object(connect, "client"):
            assert client.post(self.URL).status_code == 409


class TestRefresh:
    URL = "/api/billing/stripe/refresh/"

    def test_an_admin_gets_the_fresh_status(self, api_client, make_member, connected):
        client = as_role(api_client, make_member, connected, Role.ADMIN)

        with mock.patch.object(connect, "refresh_account", side_effect=lambda o: o) as refresh:
            response = client.post(self.URL)

        assert response.status_code == 200
        assert response.data["connected"] is True
        assert response.data["charges_enabled"] is True
        refresh.assert_called_once_with(connected)

    def test_the_owner_can_too(self, api_client, make_member, connected):
        client = as_role(api_client, make_member, connected, Role.OWNER)

        with mock.patch.object(connect, "refresh_account", side_effect=lambda o: o):
            assert client.post(self.URL).status_code == 200

    @pytest.mark.parametrize("role", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_below_admin_is_refused(self, api_client, make_member, organization, role):
        client = as_role(api_client, make_member, organization, role)

        assert client.post(self.URL).status_code == 403

    def test_anonymous_is_refused(self, api_client):
        assert api_client.post(self.URL).status_code in (401, 403)

    def test_is_a_503_without_stripe(self, api_client, make_member, connected, settings):
        settings.STRIPE_ENABLED = False
        client = as_role(api_client, make_member, connected, Role.ADMIN)

        assert client.post(self.URL).status_code == 503


# ---------------------------------------------------------------------------
# The pay page
# ---------------------------------------------------------------------------


class TestPayPage:
    def url(self, invoice):
        return f"/api/billing/pay/{connect.pay_token(invoice)}/"

    def test_shows_the_invoice_to_anyone_with_the_link(self, api_client, connected):
        invoice = IssuedInvoiceFactory(
            organization=connected,
            number="SPK-0009",
            total_cents=20000,
            bill_to_name="Pat",
            bill_to_email="pat@x.test",
            bill_to_address="1 Main St",
        )
        PaymentFactory(organization=connected, invoice=invoice, amount_cents=5000)

        response = api_client.get(self.url(invoice))

        assert response.status_code == 200
        body = response.data
        assert body["organization_name"] == connected.name
        assert body["number"] == "SPK-0009"
        assert body["bill_to_name"] == "Pat"
        assert body["total_cents"] == 20000
        assert body["paid_cents"] == 5000
        assert body["balance_cents"] == 15000
        assert body["payment_state"] == "partial"
        assert body["payable_reason"] is None

    def test_says_nothing_it_should_not(self, api_client, connected):
        invoice = IssuedInvoiceFactory(
            organization=connected, bill_to_email="pat@x.test", bill_to_address="1 Main St"
        )

        body = api_client.get(self.url(invoice)).data

        for secret in ("bill_to_email", "bill_to_address", "customer", "id", "organization"):
            assert secret not in body
        assert "pat@x.test" not in str(body)
        assert "1 Main St" not in str(body)

    def test_gives_the_reason_when_it_cannot_be_paid(self, api_client, organization):
        organization.name = "Rival Cleaners"
        organization.save()
        invoice = IssuedInvoiceFactory(organization=organization)

        body = api_client.get(self.url(invoice)).data

        assert body["payable_reason"] == "Rival Cleaners does not take card payments online."

    def test_a_bad_token_is_a_404(self, api_client):
        assert api_client.get("/api/billing/pay/not-a-token/").status_code == 404

    def test_a_token_for_a_deleted_invoice_is_a_404(self, api_client, connected):
        """
        Soft-deleted: the default manager must hide it, and the token must not
        see past. (An issued invoice refuses `.delete()`, so the row is
        stamped directly -- the question is what the token does, not how.)
        """
        from django.utils import timezone

        from billing.models import Invoice

        invoice = IssuedInvoiceFactory(organization=connected)
        token = connect.pay_token(invoice)
        Invoice.all_objects.filter(pk=invoice.pk).update(deleted_at=timezone.now())

        assert api_client.get(f"/api/billing/pay/{token}/").status_code == 404

    def test_both_endpoints_are_throttled_with_a_rate_that_resolves(self):
        """
        The scope alone proves nothing: with `throttle_classes` removed the
        attribute would still be there, and a scope with no rate in the
        running settings raises on the first request (which is how the pay
        page came to 500 under `settings.local`).
        """
        from rest_framework.throttling import ScopedRateThrottle

        from billing.views import PayInvoiceCheckoutView, PayInvoiceView

        for view in (PayInvoiceView, PayInvoiceCheckoutView):
            assert ScopedRateThrottle in view.throttle_classes
            throttle = ScopedRateThrottle()
            throttle.scope = view.throttle_scope
            assert throttle.get_rate()  # ImproperlyConfigured if the scope has no rate


class TestCheckout:
    def url(self, invoice):
        return f"/api/billing/pay/{connect.pay_token(invoice)}/checkout/"

    def test_opens_a_session_for_the_balance(self, api_client, connected):
        invoice = IssuedInvoiceFactory(organization=connected)

        with mock.patch.object(
            connect, "create_checkout_session", return_value="https://checkout.stripe.com/x"
        ) as create:
            response = api_client.post(self.url(invoice))

        assert response.status_code == 200
        assert response.data == {"url": "https://checkout.stripe.com/x"}
        create.assert_called_once_with(invoice)

    def test_not_payable_is_a_409_with_the_reason(self, api_client, organization):
        invoice = IssuedInvoiceFactory(organization=organization)

        with mock.patch.object(connect, "client"):
            response = api_client.post(self.url(invoice))

        assert response.status_code == 409
        assert "does not take card payments" in response.data["detail"]

    def test_is_a_503_without_stripe(self, api_client, connected, settings):
        settings.STRIPE_ENABLED = False

        response = api_client.post(self.url(IssuedInvoiceFactory(organization=connected)))

        assert response.status_code == 503

    def test_a_bad_token_is_a_404(self, api_client):
        assert api_client.post("/api/billing/pay/nope/checkout/").status_code == 404


# ---------------------------------------------------------------------------
# What the dispatcher's serializers say
# ---------------------------------------------------------------------------


class TestInvoiceFields:
    def test_pay_url_when_the_server_would_honour_it(self, api_client, make_member, connected):
        client = as_role(api_client, make_member, connected, Role.DISPATCHER)
        invoice = IssuedInvoiceFactory(organization=connected)

        body = client.get(f"/api/billing/invoices/{invoice.pk}/").data

        assert body["pay_url"] == connect.pay_url(invoice)
        assert body["pay_url"].startswith("https://app.example.test/pay/")
        assert body["overpaid_cents"] == 0

    def test_no_pay_url_when_it_would_not(self, api_client, make_member, organization):
        client = as_role(api_client, make_member, organization, Role.DISPATCHER)
        invoice = IssuedInvoiceFactory(organization=organization)

        assert client.get(f"/api/billing/invoices/{invoice.pk}/").data["pay_url"] is None

    def test_overpaid_cents_is_published(self, api_client, make_member, connected):
        client = as_role(api_client, make_member, connected, Role.DISPATCHER)
        invoice = IssuedInvoiceFactory(organization=connected, total_cents=5000)
        PaymentFactory(organization=connected, invoice=invoice, amount_cents=2000)
        services.record_provider_payment(
            invoice,
            provider="stripe",
            provider_reference="pi_over",
            amount_cents=5000,
            received_on=connected.today(),
        )

        body = client.get(f"/api/billing/invoices/{invoice.pk}/").data

        assert body["balance_cents"] == 0
        assert body["overpaid_cents"] == 2000


class TestPaymentFields:
    @pytest.fixture
    def card(self, connected):
        invoice = IssuedInvoiceFactory(organization=connected, total_cents=5000)
        return services.record_provider_payment(
            invoice,
            provider="stripe",
            provider_reference="pi_card",
            amount_cents=5000,
            received_on=connected.today(),
            fee_cents=175,
        )

    def test_a_card_payment_shows_its_fee_and_cannot_be_voided_by_hand(
        self, api_client, make_member, connected, card
    ):
        client = as_role(api_client, make_member, connected, Role.DISPATCHER)

        body = client.get(f"/api/billing/payments/{card.pk}/").data

        assert body["provider"] == "stripe"
        assert body["fee_cents"] == 175
        assert body["can_void"] is False
        assert body["dispute_status"] == ""

    def test_a_recorded_payment_can_be(self, api_client, make_member, organization):
        client = as_role(api_client, make_member, organization, Role.DISPATCHER)
        payment = PaymentFactory(organization=organization)

        body = client.get(f"/api/billing/payments/{payment.pk}/").data

        assert body["can_void"] is True
        assert body["fee_cents"] is None

    def test_but_not_twice(self, api_client, make_member, organization):
        client = as_role(api_client, make_member, organization, Role.DISPATCHER)
        payment = PaymentFactory(organization=organization)
        services.void_payment(payment, reason="bounced")

        assert client.get(f"/api/billing/payments/{payment.pk}/").data["can_void"] is False

    def test_voiding_a_card_payment_is_a_409(self, api_client, make_member, connected, card):
        client = as_role(api_client, make_member, connected, Role.DISPATCHER)

        response = client.post(
            f"/api/billing/payments/{card.pk}/void/", {"reason": "oops"}, format="json"
        )

        assert response.status_code == 409
        assert "refund" in response.data["detail"]
        card.refresh_from_db()
        assert not card.is_void

    def test_the_service_refuses_too(self, card):
        with pytest.raises(ConflictError):
            services.assert_voidable_by_hand(card)


# ---------------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------------


class TestEmailPayButton:
    @pytest.fixture
    def issued(self, org, customer, service):
        job = completed_job(org, customer, service, price_cents=15000)
        return services.issue_invoice(services.draft_invoice(customer=customer, jobs=[job]))

    def test_carries_the_pay_link_when_the_organization_takes_cards(self, issued, org):
        org.stripe_account_id = ACCOUNT_ID
        org.stripe_charges_enabled = True
        org.save()
        issued.refresh_from_db()

        services.send_invoice(issued)

        body = mail.outbox[0].body
        assert "Pay online" in body
        assert connect.pay_url(issued) in body

    def test_has_no_button_otherwise(self, issued):
        services.send_invoice(issued)

        assert "Pay online" not in mail.outbox[0].body

    def test_has_no_button_without_stripe_on_the_server(self, issued, org, settings):
        settings.STRIPE_ENABLED = False
        org.stripe_account_id = ACCOUNT_ID
        org.stripe_charges_enabled = True
        org.save()

        services.send_invoice(issued)

        assert "Pay online" not in mail.outbox[0].body

    def test_another_organization_never_gets_this_ones_link(self, issued, org):
        """The link names the invoice; the invoice names its organization."""
        rival = OrganizationFactory(stripe_account_id="acct_rival", stripe_charges_enabled=True)
        issued.refresh_from_db()

        services.send_invoice(issued)

        assert "Pay online" not in mail.outbox[0].body
        assert rival.name not in mail.outbox[0].body
