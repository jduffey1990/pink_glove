"""
`record_provider_payment` and `overpaid_cents`, directly.

The webhook tests cover the paths a real event takes; these are the edges a
webhook should never reach but the service must still answer for.
"""

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from app.exceptions import ConflictError
from billing.enums import InvoiceStatus, PaymentMethod
from billing.models import Payment
from billing.services import overpaid_cents, record_provider_payment
from billing.tests.factories import InvoiceFactory, IssuedInvoiceFactory, PaymentFactory
from scheduling.tests.factories import OrganizationFactory

pytestmark = pytest.mark.django_db


def record(invoice, **overrides):
    params = {
        "provider": "stripe",
        "provider_reference": "pi_1",
        "amount_cents": invoice.total_cents,
        "received_on": invoice.organization.today(),
    }
    return record_provider_payment(invoice, **{**params, **overrides})


class TestRecordProviderPayment:
    def test_a_draft_is_refused(self):
        with pytest.raises(ConflictError):
            record(InvoiceFactory(total_cents=1000), amount_cents=1000)

    def test_nothing_is_refused(self):
        with pytest.raises(ValidationError):
            record(IssuedInvoiceFactory(), amount_cents=0)

    def test_a_void_invoice_still_takes_the_money_and_says_so(self, caplog):
        """It happened; the dashboard refund is how it is put right."""
        invoice = IssuedInvoiceFactory(status=InvoiceStatus.VOID, total_cents=5000)

        with caplog.at_level("WARNING"):
            payment = record(invoice)

        assert payment.invoice == invoice
        assert "void invoice" in caplog.text

    def test_is_idempotent_on_the_reference_even_when_the_first_row_is_void(self):
        invoice = IssuedInvoiceFactory(total_cents=5000)
        first = record(invoice)
        first.voided_at = timezone.now()
        first.save()

        assert record(invoice) == first
        assert Payment.objects.count() == 1

    def test_the_row_is_a_card_payment_nobody_recorded(self):
        payment = record(IssuedInvoiceFactory(total_cents=5000), fee_cents=175)

        assert payment.method == PaymentMethod.CARD
        assert payment.recorded_by is None
        assert payment.fee_cents == 175
        assert payment.tip_cents == 0


class TestOverpaidCents:
    def test_zero_when_paid_to_the_cent_or_less(self):
        invoice = IssuedInvoiceFactory(total_cents=5000)
        PaymentFactory(organization=invoice.organization, invoice=invoice, amount_cents=5000)

        assert overpaid_cents(invoice) == 0

    def test_the_excess_when_a_provider_overpaid(self):
        invoice = IssuedInvoiceFactory(total_cents=5000)
        PaymentFactory(organization=invoice.organization, invoice=invoice, amount_cents=2000)
        record(invoice, amount_cents=5000)

        assert overpaid_cents(invoice) == 2000

    def test_zero_for_anything_not_issued(self):
        organization = OrganizationFactory()
        assert overpaid_cents(InvoiceFactory(organization=organization)) == 0
