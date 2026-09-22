"""
The Stripe ledger and the Connect fields, at the database.

Two idempotency keys matter here: an event is unique per (id, account), and
a Stripe account belongs to one organization. Both are what let the webhook
trust a replay to do nothing.
"""

import pytest
from django.db import IntegrityError

from billing.enums import StripeEventStatus
from billing.models import StripeEvent
from billing.tests.factories import PaymentFactory, StripeEventFactory
from scheduling.tests.factories import OrganizationFactory

pytestmark = pytest.mark.django_db


class TestStripeEventLedger:
    def test_arrives_as_received_with_no_organization(self):
        event = StripeEventFactory()

        assert event.status == StripeEventStatus.RECEIVED
        assert event.organization is None
        assert event.processed_at is None

    def test_the_same_event_id_for_the_same_account_is_refused(self):
        StripeEventFactory(event_id="evt_1", account="acct_a")

        with pytest.raises(IntegrityError):
            StripeEventFactory(event_id="evt_1", account="acct_a")

    def test_the_same_event_id_for_a_different_account_is_a_different_event(self):
        """Stripe scopes ids per account; the ledger has to as well."""
        StripeEventFactory(event_id="evt_1", account="acct_a")
        StripeEventFactory(event_id="evt_1", account="acct_b")

        assert StripeEvent.objects.filter(event_id="evt_1").count() == 2

    def test_a_platform_event_has_an_empty_account_and_is_still_unique(self):
        StripeEventFactory(event_id="evt_p", account="")

        with pytest.raises(IntegrityError):
            StripeEventFactory(event_id="evt_p", account="")

    def test_the_organization_cannot_be_deleted_from_under_its_events(self):
        """PROTECT: the ledger outlives the tenant's row, as an audit trail should."""
        from django.db.models import ProtectedError

        organization = OrganizationFactory()
        StripeEventFactory(organization=organization)

        with pytest.raises(ProtectedError):
            organization.hard_delete()


class TestOneOrganizationPerStripeAccount:
    def test_two_organizations_cannot_share_an_account(self):
        OrganizationFactory(stripe_account_id="acct_shared")

        with pytest.raises(IntegrityError):
            OrganizationFactory(stripe_account_id="acct_shared")

    def test_many_organizations_may_have_none(self):
        OrganizationFactory(stripe_account_id="")
        OrganizationFactory(stripe_account_id="")

    def test_connected_means_there_is_an_account(self):
        assert OrganizationFactory(stripe_account_id="").stripe_connected is False
        assert OrganizationFactory(stripe_account_id="acct_x").stripe_connected is True
        assert OrganizationFactory(stripe_account_id="acct_y").stripe_charges_enabled is False


class TestPaymentProviderFields:
    def test_a_recorded_payment_has_no_fee_and_no_dispute(self):
        payment = PaymentFactory()

        assert payment.fee_cents is None
        assert payment.disputed_at is None
        assert payment.dispute_status == ""
